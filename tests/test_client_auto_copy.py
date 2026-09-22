"""自動複製的授權邊界：真實左鍵手勢、處理完成後、只取本次可見來源。"""
from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest
from textual import events
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.selection import SELECT_ALL
from textual.widgets import Button, Collapsible, OptionList, Static, TextArea
from textual.widgets.text_area import Selection

import client_app
from tests.test_client_app import _AsksApproval, _Blocks, _Engine, _wait_for_approval


pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))


def _raw(app, event_type, point, *, button=1, delta=(0, 0)):
    """Pilot.mouse_* bypasses App.on_event; enter through the real driver route."""
    event = event_type(
        None, point[0], point[1], delta[0], delta[1], button, False, False, False,
    )
    assert not event.is_forwarded
    assert app.post_message(event)
    return event


async def _mouse(app, pilot, event_type, point, *, button=1, delta=(0, 0)):
    event = _raw(app, event_type, point, button=button, delta=delta)
    await pilot.pause()
    return event


async def _drag(app, pilot, start, end, *, button=1):
    await _mouse(app, pilot, events.MouseDown, start, button=button)
    await _mouse(app, pilot, events.MouseMove, end, button=button,
                 delta=(end[0] - start[0], end[1] - start[1]))
    await _mouse(app, pilot, events.MouseUp, end, button=button)


def _point(widget, x=0, y=0):
    return widget.content_region.x + x, widget.content_region.y + y


def _capture(app, monkeypatch):
    app.copy_to_clipboard("unchanged clipboard")
    writes, notices = [], []
    monkeypatch.setattr(app._driver, "write", writes.append)
    monkeypatch.setattr(app, "notify", lambda message, **_kwargs: notices.append(message))
    return SimpleNamespace(
        writes=writes, notices=notices,
        osc52=lambda: [value for value in writes if "\x1b]52;" in value],
    )


def _encoded(text):
    return f"\x1b]52;c;{base64.b64encode(text.encode()).decode()}\a"


def test_auto_copy_raw_drag_preserves_text_and_sends_one_request_per_gesture(monkeypatch):
    """跨元件的中文／code 逐字進 OSC52，保留反白，同文的新手勢仍可再送。"""
    async def body():
        engine = _Engine()
        app = client_app.CodeTrailApp(engine)
        async with app.run_test(size=(100, 30)) as pilot:
            first = Static(client_app.Text("中文回答"))
            second = Static(client_app.Text("if ready:\n    print('好')"))
            await app.query_one("#log").mount(first, second)
            await pilot.pause()
            captured = _capture(app, monkeypatch)
            start, end = _point(first), _point(second, 15, 1)
            expected = "中文回答\nif ready:\n    print('好')"
            before = engine.session_id, list(engine.messages), list(engine.sent)
            for count in (1, 2):
                await _drag(app, pilot, start, end)
                assert app.clipboard == expected
                assert captured.osc52() == [_encoded(expected)] * count
                assert app.screen.get_selected_text() == expected
            assert captured.notices == []
            assert (engine.session_id, engine.messages, engine.sent) == before
            assert not engine.cancelled and not app.coordinator.busy

    asyncio.run(body())


def test_auto_copy_double_and_triple_click_follow_target_handler(monkeypatch):
    """阻塞目標 Click；即使 App／Screen 已刷新，也不能提前讀空或舊選取。"""
    async def body():
        trace = []
        entered, release = asyncio.Event(), asyncio.Event()

        class DelayedText(Static):
            async def _on_click(self, event):
                if event.chain >= 2:
                    entered.set()
                    await release.wait()
                await super()._on_click(event)
                event.prevent_default()
                trace.append(("target", event.chain))

        class ObservedApp(client_app.CodeTrailApp):
            def on_click(self, event):
                trace.append(("app", event.chain))
                super().on_click(event)

        app = ObservedApp(_Engine())
        app.CLICK_CHAIN_TIME_THRESHOLD = 60
        async with app.run_test(size=(100, 30)) as pilot:
            text = DelayedText(client_app.Text("double 中文"))
            sibling = Static(client_app.Text("triple code();"))
            hidden = Static(client_app.Text("hidden private fragment"))
            hidden.display = False
            await app.query_one("#log").mount(text, sibling, hidden)
            await pilot.pause()
            captured = _capture(app, monkeypatch)
            original_copy = app.copy_to_clipboard

            def copy_after_handler(value):
                chain = trace[-1][1]
                assert trace[-2:] == [("target", chain), ("app", chain)]
                original_copy(value)

            monkeypatch.setattr(app, "copy_to_clipboard", copy_after_handler)
            point = _point(text, 1)
            for chain in (1, 2, 3):
                await _mouse(app, pilot, events.MouseDown, point)
                if chain == 1:
                    await _mouse(app, pilot, events.MouseUp, point)
                    assert captured.osc52() == []
                    continue
                entered.clear()
                release.clear()
                try:
                    _raw(app, events.MouseUp, point)
                    await asyncio.wait_for(entered.wait(), 2)
                    refreshed = asyncio.Event()
                    app.call_after_refresh(refreshed.set)
                    await asyncio.wait_for(refreshed.wait(), 2)
                    assert len(captured.osc52()) == chain - 2
                finally:
                    release.set()
                await pilot.pause()
                expected = "double 中文" if chain == 2 else "double 中文\ntriple code();"
                assert app.clipboard == expected
                assert captured.osc52()[-1] == _encoded(expected)
                assert len(captured.osc52()) == chain - 1
                assert text in app.screen.selections
                assert "hidden private fragment" not in app.clipboard

    asyncio.run(body())


def test_auto_copy_container_drag_uses_final_native_selection_after_scroll(monkeypatch):
    """依原生能力从容器／本文拖曳；捲動後複製最後選取，不以版本 skip 略過。"""
    async def body():
        app = client_app.CodeTrailApp(_Engine())
        # The test starts the native scroll callback explicitly, after capturing
        # the first real-mouse selection, to avoid timing-dependent edge entry.
        app.ENABLE_SELECT_AUTO_SCROLL = False
        async with app.run_test(size=(100, 30)) as pilot:
            log = app.query_one("#log")
            lines = [Static(client_app.Text(f"line {number:02d} 中文")) for number in range(50)]
            lines[0].styles.margin = (2, 0, 0, 0)
            await log.mount(*lines)
            log.scroll_home(animate=False)
            await pilot.pause()
            captured = _capture(app, monkeypatch)
            modern_selection = hasattr(app.screen, "_select_state")
            start = _point(log, 2) if modern_selection else _point(lines[0])
            end = _point(log, 2, 8)
            if modern_selection:
                source, content_offset = app.screen.get_widget_and_offset_at(*start)
                assert source is log and content_offset is None
            await _mouse(app, pilot, events.MouseDown, start)
            await _mouse(app, pilot, events.MouseMove, end, delta=(0, 8))
            before = app.screen.get_selected_text()
            assert before and log not in app.screen.selections
            if hasattr(app.screen, "_start_auto_scroll"):
                app.screen._start_auto_scroll(log, 1, 180 / app.SELECT_AUTO_SCROLL_SPEED)
                app.screen._stop_auto_scroll()
                await pilot.pause()
                if modern_selection:
                    app.screen._update_select()
                else:
                    app.screen._update_select(app.mouse_position)
            else:
                # Textual 8.0 has no native auto-scroll timer. Continue the real
                # drag across a scrolled viewport through its MouseMove handler.
                log.scroll_to(y=3, animate=False)
                await pilot.pause()
                await _mouse(app, pilot, events.MouseMove, end)
            final = app.screen.get_selected_text()
            assert final and final != before
            await _mouse(app, pilot, events.MouseUp, end)
            assert app.clipboard == final and captured.osc52() == [_encoded(final)]
            assert app.screen.get_selected_text() == final

    asyncio.run(body())


def test_auto_copy_ignores_non_selection_events_and_lost_mouse_moves(monkeypatch):
    """程式／刷新／重播／非左鍵／孤立 release 不授權；漏移動不得送舊反白。"""
    async def body():
        app = client_app.CodeTrailApp(_Engine())
        async with app.run_test(size=(100, 30)) as pilot:
            text = Static(client_app.Text("previous selection"))
            stream = client_app.AssistantBlock()
            await app.query_one("#log").mount(text, stream)
            await pilot.pause()
            captured = _capture(app, monkeypatch)
            app.screen.selections = {text: SELECT_ALL}
            prompt = app.query_one("#prompt", client_app.PromptInput)
            prompt.text = "private draft"
            prompt.selection = Selection((0, 0), (0, 7))
            prompt.load_history(["recalled prompt"])
            prompt._recall(-1)
            stream.append("stream delta 中文")
            app._replay_history([{"role": "assistant", "content": "replay text"}], clear=False)
            app.refresh()
            await pilot.pause()
            stream.finish()
            await pilot.pause()
            assert captured.osc52() == []
            point, end = _point(text), _point(text, 8)
            await _mouse(app, pilot, events.MouseUp, end)  # No paired raw down.
            await _drag(app, pilot, point, end, button=3)
            assert captured.osc52() == []
            app.screen.selections = {text: SELECT_ALL}
            await _mouse(app, pilot, events.MouseDown, point)
            await _mouse(app, pilot, events.MouseUp, end)  # Terminal lost every move.
            assert captured.osc52() == []
            app.screen.selections = {text: SELECT_ALL}
            await _mouse(app, pilot, events.MouseDown, point)
            await _mouse(app, pilot, events.MouseUp, point)  # A plain click.
            assert captured.osc52() == []
            assert app.clipboard == "unchanged clipboard" and captured.notices == []

    asyncio.run(body())


def test_auto_copy_control_gestures_do_not_copy_existing_selection(monkeypatch):
    """allow_select 而非控制元件黑名單；按鈕、選單、展開標題與捲軸都不外洩旧選取。"""
    class FutureControl(Static):
        ALLOW_SELECT = False

    async def body():
        app = client_app.CodeTrailApp(_Engine())
        async with app.run_test(size=(100, 40)) as pilot:
            text = Static(client_app.Text("private previous selection"))
            button = Button("control")
            options = OptionList("one", "two")
            options.styles.height = 4
            fold = Collapsible(Static("hidden body"), title="expand")
            future = FutureControl("unlisted control")
            tall = Static(client_app.Text("scroll line\n" * 100))
            log = app.query_one("#log")
            await log.mount(text, button, options, fold, future, tall)
            log.scroll_home(animate=False)
            await pilot.pause()
            captured = _capture(app, monkeypatch)
            for control in (button, options, fold.query_one("CollapsibleTitle"), future,
                            log.vertical_scrollbar):
                assert not control.allow_select
                app.screen.selections = {text: SELECT_ALL}
                point = _point(control)
                await _drag(app, pilot, point, (point[0], point[1] + 1))
                assert captured.osc52() == [], type(control).__name__
            assert app.clipboard == "unchanged clipboard"

    asyncio.run(body())


def test_auto_copy_reads_only_originating_editor_without_focus_fallback(monkeypatch):
    """輸入框手勢只讀起點 editor；焦點／Screen 的其它 NDA 草稿不構成備援。"""
    async def body():
        app = client_app.CodeTrailApp(_Engine())
        async with app.run_test(size=(100, 30)) as pilot:
            origin = TextArea("code 中文\n  next()")
            origin.styles.height = 5
            text = Static(client_app.Text("other screen selection"))
            await app.query_one("#log").mount(origin, text)
            prompt = app.query_one("#prompt", client_app.PromptInput)
            prompt.text = "other private draft"
            await pilot.pause()
            captured = _capture(app, monkeypatch)
            start, end = _point(origin), _point(origin, 8, 1)
            await _mouse(app, pilot, events.MouseDown, start)
            await _mouse(app, pilot, events.MouseMove, end)
            selected = origin.selected_text
            assert selected == "code 中文\n  next()"
            prompt.selection = Selection((0, 0), (0, 5))
            prompt.focus()
            app.screen.selections = {text: SELECT_ALL}
            await _mouse(app, pilot, events.MouseUp, end)
            assert app.clipboard == selected and captured.osc52() == [_encoded(selected)]
            assert origin.selected_text == selected and origin.text == selected
            captured.writes.clear()
            await _mouse(app, pilot, events.MouseDown, start)
            # A real move which stays at the same editor cursor has no selection.
            end = (start[0] - 1, start[1])
            await _mouse(app, pilot, events.MouseMove, end)
            assert not origin.selected_text
            prompt.selection = Selection((0, 0), (0, 5))
            prompt.focus()
            app.screen.selections = {text: SELECT_ALL}
            await _mouse(app, pilot, events.MouseUp, end)
            assert captured.osc52() == [] and app.clipboard == selected

    asyncio.run(body())


@pytest.mark.parametrize("invalidated", (
    "hidden", "hidden_parent", "removed", "replacement", "modal_roundtrip", "session",
))
def test_auto_copy_invalidated_source_cannot_finish_old_gesture(monkeypatch, invalidated):
    """按下到放開間來源隱藏、移除、Markdown 替換、換 modal/session 一律失效。"""
    async def body():
        app = client_app.CodeTrailApp(_Engine())
        async with app.run_test(size=(100, 30)) as pilot:
            block = client_app.AssistantBlock()
            block.append("old stream selection")
            parent = Vertical(block)
            parent.styles.height = "auto"
            await app.query_one("#log").mount(parent)
            await pilot.pause()
            origin = block._stream
            start, end = _point(origin), _point(origin, 8)
            captured = _capture(app, monkeypatch)
            await _mouse(app, pilot, events.MouseDown, start)
            await _mouse(app, pilot, events.MouseMove, end)
            assert app.screen.get_selected_text()
            if invalidated == "hidden":
                origin.visible = False
            elif invalidated == "hidden_parent":
                parent.display = False
            elif invalidated == "removed":
                await origin.remove()
            elif invalidated == "replacement":
                block.finish()
            elif invalidated == "modal_roundtrip":
                await app.push_screen(ModalScreen())
                await app.pop_screen()
            else:
                app._cmd_new("")
            await _mouse(app, pilot, events.MouseUp, end)
            assert captured.osc52() == [] and app.clipboard == "unchanged clipboard"

    asyncio.run(body())


def test_auto_copy_old_queued_bubbles_cannot_finish_new_gesture(monkeypatch):
    """兩次原始 release 都已排隊時，第一個晚到的 bubble 不能讀第二次的選取。"""
    async def body():
        first_entered, second_entered = asyncio.Event(), asyncio.Event()
        first_release, second_release = asyncio.Event(), asyncio.Event()
        first_bubbled, second_forwarded = asyncio.Event(), asyncio.Event()

        class HeldText(Static):
            releases = 0

            async def _on_mouse_up(self, event):
                self.releases += 1
                (first_entered if self.releases == 1 else second_entered).set()
                await (first_release if self.releases == 1 else second_release).wait()
                await super()._on_mouse_up(event)
                event.prevent_default()

        class ObservedApp(client_app.CodeTrailApp):
            raw_releases = 0
            bubbled_releases = 0

            async def on_event(self, event):
                raw_up = isinstance(event, events.MouseUp) and not event.is_forwarded
                await super().on_event(event)
                if raw_up:
                    self.raw_releases += 1
                    if self.raw_releases == 2:
                        second_forwarded.set()

            def on_mouse_up(self, event):
                super().on_mouse_up(event)
                self.bubbled_releases += 1
                if self.bubbled_releases == 1:
                    first_bubbled.set()

        app = ObservedApp(_Engine())
        app.CLICK_CHAIN_TIME_THRESHOLD = 0
        async with app.run_test(size=(100, 30)) as pilot:
            text = HeldText(client_app.Text("0123456789"))
            await app.query_one("#log").mount(text)
            await pilot.pause()
            captured = _capture(app, monkeypatch)
            start, end = _point(text), _point(text, 4)
            await _mouse(app, pilot, events.MouseDown, start)
            await _mouse(app, pilot, events.MouseMove, end)
            try:
                _raw(app, events.MouseUp, end)
                await asyncio.wait_for(first_entered.wait(), 2)
                # Construct every raw event before forwarding either release's
                # clone: raw event.time cannot be used as a gesture boundary.
                _raw(app, events.MouseDown, _point(text, 2))
                _raw(app, events.MouseMove, end, delta=(2, 0))
                _raw(app, events.MouseUp, end)
                await asyncio.wait_for(second_forwarded.wait(), 2)
                first_release.set()
                await asyncio.wait_for(first_bubbled.wait(), 2)
                await asyncio.wait_for(second_entered.wait(), 2)
                assert captured.osc52() == []
                second_release.set()
                await pilot.pause()
                assert app.clipboard == "234"
                assert captured.osc52() == [_encoded("234")]
            finally:
                first_release.set()
                second_release.set()

    asyncio.run(body())


@pytest.mark.parametrize("state", ("busy", "approval_cancel", "approval_deny"))
def test_auto_copy_during_turn_and_approval_preserves_session_and_controls(monkeypatch, state):
    """忙碌／核准內可拖曳複製；零歷史寫入、零核准，Ctrl-C／Esc 原義保留。"""
    async def body():
        engine = _Blocks() if state == "busy" else _AsksApproval()
        app = client_app.CodeTrailApp(engine)
        async with app.run_test(size=(100, 35)) as pilot:
            assert app.submit("read only public text")
            try:
                if state == "busy":
                    assert engine.entered.wait(5)
                    screen = app.screen
                    source = Static(client_app.Text("in progress 中文"))
                    await app.query_one("#log").mount(source)
                else:
                    screen = await _wait_for_approval(app, pilot)
                    source = screen.query_one("#approval-detail")
                await pilot.pause()
                captured = _capture(app, monkeypatch)
                before = engine.session_id, list(engine.messages), list(engine.sent)
                monkeypatch.setattr(engine.store, "append", lambda *_args, **_kwargs:
                                    pytest.fail("自動複製不得寫入 session"))
                await _drag(app, pilot, _point(source), _point(source, 8))
                assert app.clipboard == screen.get_selected_text()
                assert captured.osc52() == [_encoded(app.clipboard)]
                assert app.screen is screen and app.coordinator.busy and not engine.cancelled
                assert (engine.session_id, engine.messages, engine.sent) == before
                if state != "busy":
                    assert engine.granted == []
                await pilot.press("escape" if state == "approval_deny" else "ctrl+c")
                if state == "busy":
                    assert engine.cancelled
                else:
                    for _ in range(30):
                        if not app.coordinator.busy:
                            break
                        await pilot.pause()
                    assert engine.granted == [False]
                    assert engine.finished.is_set() == (state == "approval_deny")
            finally:
                if state == "busy":
                    engine.release.set()
                for _ in range(30):
                    if not app.coordinator.busy:
                        break
                    await pilot.pause()
            assert not app.coordinator.busy

    asyncio.run(body())


def test_auto_copy_supports_legacy_textual8_without_selectstart_import(monkeypatch):
    """支援的 Textual 8 不一定有 SelectStart；舊 tuple 仍須绑定這一次原始手勢。"""
    import importlib.util
    import sys

    import textual.selection as native_selection
    from textual.screen import Screen

    # Load our actual module independently: neither imports nor App classes in
    # other tests are replaced. The installed Screen already holds its imports.
    monkeypatch.delattr(native_selection, "SelectStart", raising=False)
    name = "_codetrail_textual8_compat_regression"
    spec = importlib.util.spec_from_file_location(name, client_app.__file__)
    assert spec is not None and spec.loader is not None
    isolated = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, isolated)
    spec.loader.exec_module(isolated)

    class LegacyScreen(Screen):
        """Offline bridge for Textual 8.0's native same-widget selection boundary.

        The provider exposes its documented (widget, screen, content) tuples.
        App dispatch, coordinate lookup, selection extraction, target handlers,
        bubbling and OSC52 are real; no production copy method is replaced.
        """

        _select_start = None
        _select_end = None
        empty_at = None

        @property
        def _select_state(self):
            raise AttributeError("Textual 8.0 has no _select_state")

        def clear_selection(self):
            self.selections = {}
            self._select_start = self._select_end = None

        def get_widget_and_offset_at(self, x, y):
            widget, offset = super().get_widget_and_offset_at(x, y)
            return widget, None if (x, y) == self.empty_at else offset

        def _forward_event(self, event):
            if event.is_forwarded:
                return
            if not isinstance(event, (events.MouseDown, events.MouseMove, events.MouseUp)):
                super()._forward_event(event)
                return
            event._set_forwarded()
            if isinstance(event, events.MouseDown):
                self._mouse_down_offset = event.screen_offset
                widget, offset = self.get_widget_and_offset_at(event.x, event.y)
                self._selecting = widget is not None and widget.allow_select
                if self._selecting and offset is not None:
                    self._select_start = (widget, event.screen_offset, offset)
                # A native down on blank content keeps the older tuple in 8.0.
            elif isinstance(event, events.MouseMove):
                self._handle_mouse_move(event)
                widget, offset = self.get_widget_and_offset_at(event.x, event.y)
                if self._selecting and widget is not None and offset is not None:
                    self._select_end = (widget, event.screen_offset, offset)
                    if self._select_start is not None:
                        start_widget, _point, start_offset = self._select_start
                        assert start_widget is widget, "bridge only models native same-widget drags"
                        self.selections = {
                            widget: native_selection.Selection.from_offsets(start_offset, offset),
                        }
                return
            else:
                if self._mouse_down_offset == event.screen_offset:
                    self.clear_selection()
                self._mouse_down_offset = None
                self._selecting = False
            widget, region = self.get_widget_at(event.x, event.y)
            if widget is self:
                self.post_message(event)
            else:
                widget._forward_event(event._apply_offset(-region.x, -region.y))

    # This registry belongs to the subclass; the installed Screen is untouched.
    LegacyScreen._reactives.pop("_select_state", None)

    class LegacyApp(isolated.CodeTrailApp):
        def get_default_screen(self):
            return LegacyScreen(id="_default")

    async def body():
        engine = _Engine()
        app = LegacyApp(engine)
        app.CLICK_CHAIN_TIME_THRESHOLD = 0
        async with app.run_test(size=(100, 30)) as pilot:
            source = Static(client_app.Text("public current text"))
            other = Static(client_app.Text("another source"))
            await app.query_one("#log").mount(source, other)
            await pilot.pause()
            screen = app.screen
            assert not hasattr(screen, "_select_state")
            captured = _capture(app, monkeypatch)
            start, end = _point(source), _point(source, 6)
            await _drag(app, pilot, start, end)
            assert app.clipboard == "public" and captured.osc52() == [_encoded("public")]
            assert screen.get_selected_text() == "public"
            captured.writes.clear()

            assert screen._select_start is not None
            screen.empty_at = start
            await _mouse(app, pilot, events.MouseDown, start)
            assert screen._select_start is None, "raw Down must clear the old native selection first"
            await _mouse(app, pilot, events.MouseMove, _point(source, 8))
            await _mouse(app, pilot, events.MouseUp, _point(source, 8))
            assert captured.osc52() == [] and app.clipboard == "public"
            screen.empty_at = None

            # A new raw press owns the screen before the older release bubbles.
            # It must not copy either the old source or the newly pressed one.
            await _mouse(app, pilot, events.MouseDown, start)
            await _mouse(app, pilot, events.MouseMove, end)
            _raw(app, events.MouseUp, end)
            _raw(app, events.MouseDown, _point(other))
            await pilot.pause()
            await _mouse(app, pilot, events.MouseUp, _point(other))
            assert captured.osc52() == [] and app.clipboard == "public"

            await _mouse(app, pilot, events.MouseDown, start)
            await _mouse(app, pilot, events.MouseUp, end)  # No new MouseMove evidence.
            assert captured.osc52() == []

            screen.selections = {source: SELECT_ALL}
            await pilot.pause()  # Programmatic selection alone is not authorization.
            assert captured.osc52() == []
            assert engine.messages == [] and engine.sent == [] and not engine.cancelled

    asyncio.run(body())


@pytest.mark.parametrize("gesture", ("drag", "double"))
@pytest.mark.parametrize("following", ("hover", "key", "paste"))
def test_auto_copy_released_gesture_survives_queued_hover_key_and_paste(
    monkeypatch, gesture, following,
):
    """放開後的普通輸入先於 bubble 抵達，也不能丟掉已完成手勢的唯一複製。"""
    class FocusableText(Static, can_focus=True):
        pass

    async def body():
        app = client_app.CodeTrailApp(_Engine())
        app.CLICK_CHAIN_TIME_THRESHOLD = 60 if gesture == "double" else 0
        async with app.run_test(size=(100, 30)) as pilot:
            expected = "selected 中文 code()"
            source = FocusableText(client_app.Text(expected))
            await app.query_one("#log").mount(source)
            source.focus()
            await pilot.pause()
            captured = _capture(app, monkeypatch)
            start = _point(source)
            end = (source.content_region.right - 1, start[1])
            if gesture == "double":
                end = start
                await _mouse(app, pilot, events.MouseDown, start)
                await _mouse(app, pilot, events.MouseUp, start)
            # Deliberately no pause between these raw driver events. App must
            # retain the released gesture until its target's event bubbles back.
            _raw(app, events.MouseDown, start)
            if gesture == "drag":
                _raw(app, events.MouseMove, end, delta=(end[0] - start[0], 0))
            _raw(app, events.MouseUp, end)
            if following == "hover":
                _raw(app, events.MouseMove, (end[0] - 1, end[1]), button=0, delta=(-1, 0))
            elif following == "key":
                app.post_message(events.Key("f9", None))
            else:
                app.post_message(events.Paste("next draft"))
            await pilot.pause()
            assert app.clipboard == expected
            assert captured.osc52() == [_encoded(expected)]
            assert app.screen.get_selected_text() == expected
            assert captured.notices == []
            assert app.engine.messages == [] and app.engine.sent == []

    asyncio.run(body())
