"""Completed/replayed answers must retain Textual's native mouse selection."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.selection import SELECT_ALL
from textual.widget import Widget
from textual.widgets import Markdown

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import client_app  # noqa: E402

pytestmark = pytest.mark.smoke


class _SelectionApp(App[None]):
    def __init__(self, block: client_app.AssistantBlock) -> None:
        super().__init__()
        self.block = block
        self.clicked_links: list[str] = []

    def compose(self) -> ComposeResult:
        yield VerticalScroll(self.block, id="log")

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        self.clicked_links.append(event.href)


async def _drag_over(pilot, widget: Widget) -> str | None:
    """Use terminal cell coordinates, including CJK width and visual wrapping."""
    region = widget.content_region
    assert region.width > 1 and region.height > 0
    start = (region.x, region.y)
    end = (region.right - 1, region.bottom - 1)
    await pilot.mouse_down(offset=start)
    await pilot.hover(offset=end)
    await pilot.mouse_up(offset=end)
    return pilot.app.screen.get_selected_text()


def test_finished_and_replayed_assistant_text_is_mouse_selectable(monkeypatch):
    prose = (
        "中文回答 remains selectable when this paragraph wraps across several "
        "terminal lines, including punctuation: [] {} = +."
    )
    code = 'if (ready) {\n    send("中文");\n}'
    link = "https://example.invalid/reference"
    source = f"## Answer\n\n{prose}\n\n```c\n{code}\n```\n\n[reference]({link})"

    # Session replay constructs finished widgets before there is an active App.
    replay = client_app.AssistantBlock()
    replay.finish(source)
    assert replay.text == source

    class FinishDuringMount(client_app.AssistantBlock):
        def on_mount(self) -> None:
            # Children are already attached, but is_mounted is set only after
            # this handler. A guard on the parent's is_mounted loses this finish.
            assert not self.is_mounted
            self.finish(source)

    async def body():
        streaming = client_app.AssistantBlock()
        app = _SelectionApp(streaming)
        opened_links: list[str] = []
        monkeypatch.setattr(app, "open_url", opened_links.append)
        async with app.run_test(size=(42, 30)) as pilot:
            streaming.append(prose[:19])
            streaming.append(prose[19:])
            await pilot.pause()
            assert streaming.content_region.height > 1, "paragraph did not wrap"
            assert await _drag_over(pilot, streaming) == prose
            selected_stream_widgets = tuple(app.screen.selections)
            assert selected_stream_widgets

            streaming.finish()
            await pilot.pause()
            assert not app.screen.get_selected_text(), "invisible stream selection survived finish"
            assert await _drag_over(pilot, streaming) == prose, (
                "finishing an answer made the visible paragraph unselectable"
            )
            assert all(not widget.is_attached for widget in selected_stream_widgets), (
                "the old selected stream remains attached after Markdown replaces it"
            )
            # A contentless parent must not contribute a stray blank line when
            # the selection spans several child Markdown blocks.
            assert streaming.get_selection(SELECT_ALL) is None
            streaming.append("must not appear after completion")
            assert streaming.text == prose

            log = app.query_one("#log", VerticalScroll)
            for case in ("replay", "immediate", "during_mount"):
                app.screen.clear_selection()
                await log.remove_children()
                if case == "replay":
                    block = replay
                    await log.mount(block)
                elif case == "immediate":
                    block = client_app.AssistantBlock()
                    block.append("initial delta")
                    mounted = log.mount(block)
                    # The TYPE_TEXT-only path finishes in the same handler as
                    # mount(), before the block's compose() has run.
                    block.finish(source)
                    await mounted
                else:
                    block = FinishDuringMount()
                    await log.mount(block)
                await pilot.pause()
                markdown = block.query_one(Markdown)
                assert markdown.source == source
                assert block.text == source
                assert len(markdown.query("MarkdownH2")) == 1
                paragraph = markdown.query_one("MarkdownParagraph")
                assert paragraph.content_region.height > 1
                assert await _drag_over(pilot, paragraph) == prose, case
                fence = markdown.query_one("MarkdownFence #code-content")
                assert await _drag_over(pilot, fence) == code, case
                link_widget = list(markdown.query("MarkdownParagraph"))[-1]
                region = link_widget.content_region
                await pilot.click(offset=(region.x + 1, region.y))
                assert app.clicked_links[-1:] == [link], "the rendered link was not clicked"
                assert opened_links == [], "answer links must not launch an external application"

    asyncio.run(body())
