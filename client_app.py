#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_app — `aicode` 的全螢幕終端介面(Textual)。

版面刻意只有三塊:**一條對話流**、底部輸入框、一行狀態列。沒有側欄、沒有
分頁——這個工具的使用情境是 SSH 進一台機器問一個 repo,多出來的每一塊都是
要維護的東西。

畫面上的保證(對應 AGENTS.md §2):

* **核准框完整顯示參數**(含整份 patch)且可捲動。截斷過的核准等於沒有核准。
  回傳只認真的 ``bool``:``bool("false")`` 是 True。
* **中斷與拒絕是兩件事**。核准框裡的 Esc / 拒絕只拒絕**這一個工具**,這一輪
  繼續;Ctrl-C 中斷**整輪**(核准框開著時也一樣)。兩者都走
  :mod:`client_turns` 的協調器,所以「送出後立刻中斷」與「等核准中中斷」
  這兩個 engine 自己看不到的狀態也涵蓋得到。
* **不直接 print**。Textual 接管畫面之後任何 stdout / stderr 都會把畫面打壞,
  所以這裡所有輸出都是 widget;engine 的事件由背景執行緒搬進 UI 執行緒。

`engine.send()` 跑在背景執行緒(協調器管),事件回到這裡才變成 widget;核准
在那個背景執行緒裡阻塞等 UI 回答。
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Collapsible, Static, TextArea

import client_engine
import client_events
import client_paths
import client_store
import client_turns
import context_budget

HISTORY_FILENAME = "tui_history"
HISTORY_MAX_LINES = 1000
HISTORY_MAX_BYTES = 4 * 1024 * 1024

#: 閒置時連按兩次 Ctrl-C 才離開,而且要在這段時間內。
DOUBLE_INTERRUPT_SECONDS = 2.0

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: 斜線指令 → 一行說明。輸入框的補全與 `/help` 用的是**同一份**表:兩份會漂移。
COMMANDS: tuple[tuple[str, str], ...] = (
    ("/help", "這份說明"),
    ("/new", "開一個新對話"),
    ("/sessions", "列出這個專案的既有對話"),
    ("/resume", "接續一個既有對話(/resume <id>)"),
    ("/compact", "立刻壓縮目前對話"),
    ("/status", "目前模型、context、壓縮模式與 session 位置"),
    ("/tools", "本輪暴露的工具(裸名,依 tools/list 順序)"),
    ("/thinking", "切換是否顯示模型的 thinking"),
    ("/exit", "離開"),
)

HELP_TAIL = (
    "其他輸入一律當成問題送給模型。\n"
    "Enter 送出、Alt+Enter 換行、↑/↓ 翻輸入歷史。\n"
    "回合進行中 Ctrl-C 中斷整輪;閒置時連按兩次 Ctrl-C 或 Ctrl-D 離開。"
)


class HistoryError(RuntimeError):
    """history 檔的位置或權限不合契約。呼叫端一律 fail-open(只是少一份歷史)。"""


def _history_error(message: str) -> HistoryError:
    return HistoryError(message)


def short(value: Any, limit: int = 60) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tool_summary(tool: str, arguments: Mapping[str, Any]) -> str:
    inner = ", ".join(f"{key}={short(arguments[key])}" for key in sorted(arguments))
    return f"{tool}({inner})"


# ============================================================
# 對話流的元件
# ============================================================
class UserMessage(Static):
    """使用者送出的那一則。"""

    def __init__(self, text: str) -> None:
        super().__init__(Text(text, style="bold"), classes="entry user")
        self.message = text


class NoticeLine(Static):
    """提示行:ingest 待辦、承諾卻沒呼叫工具、壓縮結果、中斷。"""

    def __init__(self, text: str) -> None:
        super().__init__(Text(f"• {text}"), classes="entry notice")
        self.message = text


class ErrorLine(Static):
    def __init__(self, text: str) -> None:
        super().__init__(Text(f"✗ {text}"), classes="entry error")
        self.message = text


class ReasoningBlock(Static):
    """模型的 thinking。``/thinking`` 只切換它的顯示,不影響送模內容。"""

    def __init__(self) -> None:
        super().__init__(classes="entry reasoning")
        self._buffer = ""

    def append(self, token: str) -> None:
        self._buffer += token
        self.update(Text(self._buffer, style="dim"))


class AssistantBlock(Static):
    """助理的一則回答。

    串流期間用純文字更新(每個 token 重新解析 Markdown 太貴,而且半個程式碼
    區塊會一直重排);這一則收完之後換成算好的 Markdown(含程式碼上色)。
    """

    def __init__(self) -> None:
        super().__init__(classes="entry assistant")
        self._buffer = ""
        self._finished = False

    @property
    def text(self) -> str:
        return self._buffer

    def append(self, token: str) -> None:
        if self._finished:
            return
        self._buffer += token
        self.update(Text(self._buffer))

    def finish(self, text: str = "") -> None:
        if text:
            self._buffer = text
        self._finished = True
        self.update(RichMarkdown(self._buffer) if self._buffer else Text(""))


class ToolBlock(Collapsible):
    """一次工具呼叫:一行摘要,展開看完整輸出。"""

    def __init__(self, tool: str, arguments: Mapping[str, Any], status: str) -> None:
        self._body = Static(Text(""), classes="tool-output")
        super().__init__(self._body, title="", collapsed=True, classes="entry tool")
        self.tool = tool
        self.arguments = dict(arguments)
        self.output = ""
        self.set_status(status)

    def set_status(self, status: str) -> None:
        self.status = status
        marks = {
            client_events.STATUS_COMPLETED: "✓",
            client_events.STATUS_ERROR: "✗",
            client_events.STATUS_DENIED: "⊘",
        }
        mark = marks.get(status, "·")
        self.title = f"{mark} {tool_summary(self.tool, self.arguments)} → {status}"

    def set_output(self, output: str) -> None:
        self.output = output
        self._body.update(Text(output or "(沒有輸出)"))


# ============================================================
# 核准框
# ============================================================
class ApprovalScreen(ModalScreen[bool]):
    """核准一次工具呼叫。**Esc / 拒絕只拒絕這個工具**,不是中斷整輪。"""

    BINDINGS = [
        Binding("escape", "deny", "拒絕", show=False),
        Binding("y", "allow", "允許", show=False),
        Binding("n", "deny", "拒絕", show=False),
    ]

    def __init__(self, ticket: client_turns.ApprovalTicket) -> None:
        super().__init__()
        self.ticket = ticket

    def compose(self) -> ComposeResult:
        with Vertical(id="approval"):
            yield Static(
                Text(f"需要核准:{self.ticket.request.tool}", style="bold"), id="approval-title"
            )
            with VerticalScroll(id="approval-body"):
                # render() 是完整的參數(含整份 patch)。這裡不截斷、不摘要:
                # 看不到全文的核准等於沒有核准。
                yield Static(Text(self.ticket.request.render()), id="approval-detail")
            with Horizontal(id="approval-buttons"):
                yield Button("允許 (y)", id="approval-allow", variant="success")
                yield Button("拒絕 (n / Esc)", id="approval-deny", variant="error")
            yield Static(
                Text("Esc 只拒絕這個工具,這一輪繼續;Ctrl-C 中斷整輪。", style="dim"),
                id="approval-hint",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "approval-allow")

    def action_allow(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


# ============================================================
# 輸入框
# ============================================================
class PromptInput(TextArea):
    """底部輸入框:Enter 送出、Alt+Enter 換行、↑/↓ 翻歷史、``/`` 出現補全。"""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class Changed(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.input_history: list[str] = []
        self._history_index: int | None = None
        self._draft = ""
        #: 補全清單目前反白第幾個(None = 沒有補全)。
        self.completion_index: int | None = None
        self.completions: list[str] = []

    def load_history(self, lines: Sequence[str]) -> None:
        self.input_history = [line for line in lines if line.strip()][-HISTORY_MAX_LINES:]
        self._history_index = None

    def remember(self, text: str) -> None:
        if not text.strip():
            return
        if self.input_history and self.input_history[-1] == text:
            self._history_index = None
            return
        self.input_history.append(text)
        del self.input_history[:-HISTORY_MAX_LINES]
        self._history_index = None

    # -- 補全 ------------------------------------------------------------
    def refresh_completions(self) -> None:
        text = self.text
        if text.startswith("/") and "\n" not in text and " " not in text:
            self.completions = [name for name, _ in COMMANDS if name.startswith(text)]
        else:
            self.completions = []
        if not self.completions:
            self.completion_index = None
        elif self.completion_index is None or self.completion_index >= len(self.completions):
            self.completion_index = 0

    def _apply_completion(self) -> None:
        if not self.completions or self.completion_index is None:
            return
        self.text = self.completions[self.completion_index]
        self.move_cursor(self.document.end)
        self.refresh_completions()
        self.post_message(self.Changed(self.text))

    # -- 鍵盤 ------------------------------------------------------------
    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        if key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted(self.text))
            return
        if key in ("alt+enter", "shift+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            self.post_message(self.Changed(self.text))
            return
        if key == "tab" and self.completions:
            event.prevent_default()
            event.stop()
            self._apply_completion()
            return
        if key in ("up", "down") and self.completions:
            event.prevent_default()
            event.stop()
            step = -1 if key == "up" else 1
            index = (self.completion_index or 0) + step
            self.completion_index = index % len(self.completions)
            self.post_message(self.Changed(self.text))
            return
        if key == "up" and self._at_first_line():
            event.prevent_default()
            event.stop()
            self._recall(-1)
            return
        if key == "down" and self._at_last_line():
            event.prevent_default()
            event.stop()
            self._recall(1)
            return
        await super()._on_key(event)
        self.refresh_completions()
        self.post_message(self.Changed(self.text))

    def _at_first_line(self) -> bool:
        return self.cursor_location[0] == 0

    def _at_last_line(self) -> bool:
        return self.cursor_location[0] == self.document.line_count - 1

    def _recall(self, step: int) -> None:
        if not self.input_history:
            return
        if self._history_index is None:
            if step > 0:
                return
            self._draft = self.text
            self._history_index = len(self.input_history) - 1
        else:
            index = self._history_index + step
            if index >= len(self.input_history):
                self._history_index = None
                self.text = self._draft
                self.move_cursor(self.document.end)
                self.refresh_completions()
                self.post_message(self.Changed(self.text))
                return
            self._history_index = max(0, index)
        self.text = self.input_history[self._history_index]
        self.move_cursor(self.document.end)
        self.refresh_completions()
        self.post_message(self.Changed(self.text))


# ============================================================
# App
# ============================================================
class CodeTrailApp(App[int]):
    """`aicode` 的介面。"""

    CSS = """
    Screen { layout: vertical; }
    #log { height: 1fr; padding: 0 1; }
    .entry { margin: 0 0 1 0; }
    .entry.user { color: $accent; }
    .entry.notice { color: $warning; }
    .entry.error { color: $error; }
    .entry.reasoning { color: $text-muted; }
    .entry.tool { margin: 0 0 1 0; }
    .tool-output { color: $text-muted; }
    #completions { height: auto; max-height: 8; padding: 0 1; color: $text-muted; }
    #prompt { height: auto; max-height: 10; border: round $primary; }
    #status { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    #approval {
        width: 90%; height: 80%; border: thick $warning; background: $surface; padding: 1 2;
    }
    #approval-body { height: 1fr; border: round $primary-darken-2; padding: 0 1; }
    #approval-buttons { height: auto; padding: 1 0 0 0; }
    #approval-buttons Button { margin: 0 2 0 0; }
    """

    BINDINGS = [
        # priority:輸入框自己把 ctrl+c 綁成「複製」、ctrl+d 綁成「刪字元」,
        # 不搶在前面的話中斷與離開都按不到。
        Binding("ctrl+c", "interrupt", "中斷", priority=True, show=False),
        Binding("ctrl+d", "leave", "離開", priority=True, show=False),
    ]

    def __init__(
        self,
        engine: client_engine.Engine,
        *,
        compactor: Any = None,
        banner: Sequence[str] = (),
        state_dir: Path | None = None,
        show_reasoning: bool = False,
        keep_historical_reasoning: bool = False,
    ) -> None:
        super().__init__()
        self.engine = engine
        self.compactor = compactor
        self.banner = tuple(banner)
        self.state_dir = state_dir
        self.show_reasoning = bool(show_reasoning)
        self.keep_historical_reasoning = bool(keep_historical_reasoning)
        self.coordinator = client_turns.TurnCoordinator(
            engine,
            emit=self._emit_from_worker,
            on_approval=self._approval_from_worker,
            on_approval_closed=self._approval_closed_from_worker,
            on_reasoning=self._reasoning_from_worker,
            compactor=compactor,
        )
        self._ui_thread_id = threading.get_ident()
        self._assistant: AssistantBlock | None = None
        self._reasoning: ReasoningBlock | None = None
        self._tools: dict[str, ToolBlock] = {}
        self._approval_screens: dict[str, ApprovalScreen] = {}
        self._turn_started: float | None = None
        self._spinner = 0
        self._last_interrupt = 0.0
        self._context_tokens = 0
        self.exit_code = 0
        #: 狀態列與補全面板目前顯示的字。widget 的 renderable 是 Textual 內部形狀,
        #: 讀它等於把介面測試綁在版本上。
        self.status_text = ""
        self.completion_text = ""

    # ---- 版面 ----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="log")
        yield Static("", id="completions")
        yield PromptInput(id="prompt")
        yield Static("", id="status")

    def on_mount(self) -> None:
        self._ui_thread_id = threading.get_ident()
        # MCP 的一次性警告(例如 stderr 落檔的 NDA 提醒)不得直接印:畫面已經被
        # 接管。重新 spawn 會在回合進行中再跑一次,所以要走搬運到 UI 執行緒那條路。
        mcp = getattr(self.engine, "mcp", None)
        if mcp is not None and hasattr(mcp, "on_notice"):
            mcp.on_notice = lambda message: self._from_worker(self._append_notice, message)
        prompt = self.query_one("#prompt", PromptInput)
        prompt.load_history(self._read_history())
        prompt.focus()
        for line in self.banner:
            self._append(NoticeLine(line))
        self._append(NoticeLine("輸入 /help 看指令。"))
        self.query_one("#completions", Static).display = False
        self._recount_context()
        self.set_interval(0.25, self._refresh_status)
        self._refresh_status()

    # ---- 對話流 --------------------------------------------------------
    def _append(self, widget: Widget) -> None:
        log = self.query_one("#log", VerticalScroll)
        log.mount(widget)
        log.scroll_end(animate=False)

    def _append_notice(self, message: str) -> None:
        self._append(NoticeLine(message))

    def _ensure_assistant(self) -> AssistantBlock:
        if self._assistant is None:
            self._assistant = AssistantBlock()
            self._append(self._assistant)
        return self._assistant

    def _ensure_reasoning(self) -> ReasoningBlock:
        if self._reasoning is None:
            self._reasoning = ReasoningBlock()
            self._reasoning.display = self.show_reasoning
            self._append(self._reasoning)
        return self._reasoning

    # ---- worker → UI ---------------------------------------------------
    def _from_worker(self, callback: Callable[..., None], *args: Any) -> None:
        """把背景執行緒的回呼搬進 UI 執行緒。

        協調器在自己的執行緒裡呼叫我們;直接動 widget 是 race。收尾階段
        (app 已經在關)搬不過去時安靜放掉——那不是一個要顯示的錯誤。
        """
        if threading.get_ident() == self._ui_thread_id:
            callback(*args)
            return
        try:
            self.call_from_thread(callback, *args)
        except Exception:  # noqa: BLE001 - app 正在關閉
            pass

    def _emit_from_worker(self, event: dict[str, Any]) -> None:
        self._from_worker(self.handle_event, event)

    def _reasoning_from_worker(self, token: str) -> None:
        self._from_worker(self._on_reasoning, token)

    def _approval_from_worker(self, ticket: client_turns.ApprovalTicket) -> None:
        self._from_worker(self._show_approval, ticket)

    def _approval_closed_from_worker(self, approval_id: str) -> None:
        self._from_worker(self._close_approval, approval_id)

    # ---- 事件 ----------------------------------------------------------
    def handle_event(self, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        if kind == client_events.TYPE_TEXT_DELTA:
            self._ensure_assistant().append(str(client_events.event_part(event).get("text", "")))
            self.query_one("#log", VerticalScroll).scroll_end(animate=False)
            return
        if kind == client_events.TYPE_TEXT:
            text = str(client_events.event_part(event).get("text", ""))
            block = self._ensure_assistant()
            block.finish(text if not block.text else "")
            self._assistant = None
            self._reasoning = None
            return
        if kind == client_events.TYPE_TOOL_USE:
            self._on_tool_event(event)
            return
        if kind == client_events.TYPE_NOTICE:
            self._append(NoticeLine(str(event.get("message", ""))))
            return
        if kind == client_events.TYPE_ERROR:
            self._append(ErrorLine(str(event.get("message", ""))))
            return
        if kind == client_events.TYPE_STEP_FINISH:
            self._on_step_finish(event)
            return

    def _on_tool_event(self, event: Mapping[str, Any]) -> None:
        part = client_events.event_part(event)
        state = part.get("state") or {}
        call_id = str(part.get("callID", ""))
        tool = str(part.get("tool", "?"))
        status = str(state.get("status", "?"))
        arguments = state.get("input") or {}
        block = self._tools.get(call_id)
        if block is None:
            block = ToolBlock(tool, arguments, status)
            self._tools[call_id] = block
            self._append(block)
        else:
            block.set_status(status)
        # 工具輸出**不進事件流**(那份 JSONL 是 canary / eval 的輸入,寫進去等於
        # 把整份工具輸出落到任何收集它的地方)。畫面要展開全文,就從 engine 剛
        # 記下的那一則 tool 訊息取。
        block.set_output(self._tool_output(call_id))

    def _tool_output(self, call_id: str) -> str:
        """展開區要的是**完整**輸出。

        送進模型的那一份 text 是套過 budget 的(``tool_result_adapter``);未裁切的
        核心留在 ``structuredContent``,而那份刻意只給 UI / eval。只顯示 text 的話,
        使用者在畫面上看到的是節錄,而純結構化結果的工具會顯示成「沒有輸出」。
        """
        for message in reversed(self.engine.messages):
            if message.get("role") != "tool" or message.get("tool_call_id") != call_id:
                continue
            content = message.get("content")
            parts = [content] if isinstance(content, str) and content else []
            structured = message.get("structured")
            if structured is not None:
                try:
                    rendered = json.dumps(structured, ensure_ascii=False, indent=2, sort_keys=True)
                except (TypeError, ValueError):
                    rendered = repr(structured)
                parts.append("--- structuredContent(未裁切;只給畫面與 eval)---\n" + rendered)
            return "\n\n".join(parts)
        return ""

    def _on_step_finish(self, event: Mapping[str, Any]) -> None:
        part = client_events.event_part(event)
        if not client_events.is_terminal_event(event):
            # tool-calls 的 step 是中間邊界:這一輪還沒結束。
            return
        self._recount_context()
        if self._assistant is not None:
            self._assistant.finish()
            self._assistant = None
        self._reasoning = None
        self._turn_started = None
        self._refresh_status()

    def _on_reasoning(self, token: str) -> None:
        self._ensure_reasoning().append(token)
        if self.show_reasoning:
            self.query_one("#log", VerticalScroll).scroll_end(animate=False)

    # ---- 核准 ----------------------------------------------------------
    def _show_approval(self, ticket: client_turns.ApprovalTicket) -> None:
        screen = ApprovalScreen(ticket)
        self._approval_screens[ticket.approval_id] = screen

        def _answered(granted: bool | None) -> None:
            self._approval_screens.pop(ticket.approval_id, None)
            # 只認真的 bool。dismiss 沒帶值(畫面被收掉)一律當拒絕。
            self.coordinator.answer_approval(ticket.approval_id, granted is True)

        self.push_screen(screen, _answered)

    def _close_approval(self, approval_id: str) -> None:
        """核准已經由別的路徑決定(中斷 / 逾時):把**這一個**框收掉。

        不能用 `pop_screen()`:那是「彈掉最上面那個」,不是「彈掉這一個」。
        """
        screen = self._approval_screens.pop(approval_id, None)
        if screen is None:
            return
        try:
            if screen.is_running:
                screen.dismiss(False)
        except Exception:  # noqa: BLE001 - 畫面已經不在了
            pass

    # ---- 送出 ----------------------------------------------------------
    def on_prompt_input_changed(self, message: PromptInput.Changed) -> None:
        self._refresh_completions()

    def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
        prompt = self.query_one("#prompt", PromptInput)
        text = message.text.strip()
        if not text:
            return
        if text.startswith("/"):
            self._consume_prompt(prompt, message.text)
            self._command(text)
            return
        # 送不出去(這一輪還在跑)時輸入框要**留著**原文:清掉再顯示一則其實沒送出的
        # 問題,使用者只能自己重打。
        if self.submit(text):
            self._consume_prompt(prompt, message.text)

    @staticmethod
    def _consume_prompt(prompt: "PromptInput", raw: str) -> None:
        prompt.remember(raw)
        prompt.text = ""
        prompt.refresh_completions()

    def submit(self, text: str) -> bool:
        """送一則訊息。回傳有沒有真的送出去。

        **先取回合鎖再貼 UserMessage**:反過來的話被 ``Busy`` 拒絕的那一則已經
        顯示在對話區,畫面上就有一則模型從來沒看過的問題。
        """
        try:
            self.coordinator.start_turn(text)
        except client_turns.TurnCoordinator.Busy:
            self._append(NoticeLine("這一輪還在跑;Ctrl-C 可以中斷它。"))
            self._refresh_completions()
            return False
        except Exception as exc:  # noqa: BLE001 - 送不出去不得帶走 UI
            self._append(ErrorLine(f"{type(exc).__name__}: {exc}"))
            self._refresh_completions()
            return False
        self._append(UserMessage(text))
        self._turn_started = time.monotonic()
        self._refresh_status()
        self._refresh_completions()
        return True

    # ---- 指令 ----------------------------------------------------------
    def _command(self, line: str) -> None:
        name, _, argument = line[1:].partition(" ")
        name = name.strip().lower()
        argument = argument.strip()
        handler = getattr(self, f"_cmd_{name}", None)
        if handler is None:
            self._append(NoticeLine(f"未知指令 /{name};/help 看清單。"))
            return
        handler(argument)

    def _cmd_help(self, _argument: str) -> None:
        lines = [f"  {name:<11}{description}" for name, description in COMMANDS]
        self._append(NoticeLine("指令:\n" + "\n".join(lines) + "\n" + HELP_TAIL))

    def _cmd_exit(self, _argument: str) -> None:
        if self._busy_notice("/exit"):
            return
        self._leave()

    _cmd_quit = _cmd_exit

    def _cmd_thinking(self, _argument: str) -> None:
        """只切換**畫面**。送模 payload 與摘要輸入不受影響(那是另一個設定)。"""
        self.show_reasoning = not self.show_reasoning
        for widget in self.query(ReasoningBlock):
            widget.display = self.show_reasoning
        self._append(NoticeLine(f"thinking 顯示:{'開' if self.show_reasoning else '關'}"))
        self._refresh_status()

    def _cmd_tools(self, _argument: str) -> None:
        lines = []
        for index, spec in enumerate(self.engine.tool_specs.values(), start=1):
            flag = "ro" if spec.read_only else "rw"
            lines.append(f"  {index:2d}. [{flag}] {spec.name}")
        self._append(NoticeLine("\n".join(lines) or "(沒有工具)"))

    def _busy_notice(self, what: str) -> bool:
        """回合進行中就擋下這個指令並回 True。

        ``/new`` 與 ``/resume`` 直接換掉 ``engine.session_id`` 與 ``messages``:
        在回合中做等於把還沒寫完的答案與自動壓縮落到**另一段**對話,舊對話留下
        一則沒有回答的 user,新對話多出一則沒有相鄰 user 的 assistant。
        只有 UI 執行緒會開始新回合,所以這裡的判斷不會有 idle→busy 的競態。
        """
        if not self.coordinator.busy:
            return False
        self._append(NoticeLine(f"這一輪還在跑,{what} 要等它結束;Ctrl-C 可以中斷它。"))
        return True

    def _cmd_new(self, _argument: str) -> None:
        if self._busy_notice("/new"):
            return
        try:
            self.engine.new_session()
        except Exception as exc:  # noqa: BLE001 - 建不了新 session 就留在原地
            self._append(ErrorLine(f"無法開新對話:{exc}(仍在 {self.engine.session_id})"))
            return
        self.coordinator.session_changed()
        # 工具 block 以 call id 當 key;換了對話,舊 id 不得再被新呼叫接上。
        self._tools.clear()
        self._append(NoticeLine(f"新對話:{self.engine.session_id}"))
        self._recount_context()
        self._refresh_status()

    def _cmd_sessions(self, _argument: str) -> None:
        sessions = self.engine.store.list_sessions()
        if not sessions:
            self._append(NoticeLine("這個專案還沒有已保存的對話。"))
            return
        lines = [
            f"  {info.session_id}  turns={info.turns}  {info.title}" for info in sessions[:20]
        ]
        self._append(NoticeLine("\n".join(lines)))

    def _cmd_resume(self, argument: str) -> None:
        if self._busy_notice("/resume"):
            return
        if not argument:
            self._append(NoticeLine("用法:/resume <session id>"))
            return
        try:
            self.engine.resume(argument)
        except Exception as exc:  # noqa: BLE001
            self._append(ErrorLine(f"無法接續:{exc}"))
            return
        self.coordinator.session_changed()
        self._tools.clear()
        self._append(NoticeLine(f"已接續 {argument}({len(self.engine.messages)} 則訊息)"))
        self._recount_context()
        self._refresh_status()

    def _cmd_compact(self, _argument: str) -> None:
        try:
            self.coordinator.start_compaction()
        except client_turns.TurnCoordinator.Busy:
            self._append(NoticeLine("這一輪還在跑;Ctrl-C 可以中斷它。"))
            return
        self._turn_started = time.monotonic()
        self._refresh_status()

    def _cmd_status(self, _argument: str) -> None:
        path = self.engine.store.path(self.engine.session_id)
        lines = [
            f"model={self.engine.options.model}",
            f"n_ctx={self.engine.options.n_ctx} max_output={self.engine.options.max_output_tokens}",
            f"tools={len(self.engine.tool_specs)} permission={self.engine.options.policy.name}",
            f"壓縮模式={self._compaction_mode()}",
            f"session={self.engine.session_id}",
            f"session 檔={path if path else '(不落檔)'}",
            f"專案指示={'已載入' if self._project_instructions() else '未載入'}",
            f"舊回合 reasoning={'送模' if self.keep_historical_reasoning else '不進模型'}",
        ]
        if self.engine.store_error:
            lines.append(
                f"⚠ session 落檔失敗({self.engine.store_error});這段對話只在記憶體裡。"
            )
        self._append(NoticeLine("\n".join(lines)))

    # ---- 鍵盤動作 ------------------------------------------------------
    def action_interrupt(self) -> None:
        """Ctrl-C。回合進行中 = 中斷整輪;閒置 = 連按兩次離開。"""
        # block=False:MCP 取消要等寬限期(10 秒)+ SIGTERM + 重新 spawn。
        # 同步跑在這裡就是整個畫面凍住,而且 worker 送事件用的
        # call_from_thread 也會排在後面一起卡住。
        if self.coordinator.cancel(block=False):
            return
        if self.coordinator.busy:
            # 有一輪在跑,但答案已經寫定 / 收尾中:沒有東西可取消,不得
            # 顯示成「已中斷」。
            self._append(NoticeLine("這一輪已經收尾,沒有可中斷的內容。"))
            return
        now = time.monotonic()
        if now - self._last_interrupt <= DOUBLE_INTERRUPT_SECONDS:
            self._leave()
            return
        self._last_interrupt = now
        self._append(NoticeLine("再按一次 Ctrl-C 離開(或 Ctrl-D)。"))

    def action_leave(self) -> None:
        """Ctrl-D。三種情境三種語意,不得混成「直接退出」。

        * 核准框開著 = EOF:**只拒絕這一個工具**,回合繼續(沿用舊 REPL 的
          「核准提示收到 EOF 就是拒絕」)。
        * 回合進行中:不離開。直接拆掉畫面會留下一個卡在核准上的 worker,
          而 `command_chat` 隨即關掉共用的 MCP。要停就先 Ctrl-C 中斷。
        * 閒置:存歷史然後離開。
        """
        if isinstance(self.screen, ApprovalScreen):
            self.screen.action_deny()
            return
        if self._busy_notice("Ctrl-D"):
            return
        self._leave()

    def _leave(self) -> None:
        """唯一的離開出口:先把輸入歷史落檔再退出。

        `App.on_unmount` 靠不住 —— Textual 拆畫面時子 widget 已經先移除,
        `query_one("#prompt")` 會失敗,於是正常退出的路徑一個 byte 都沒寫。
        """
        self._save_history()
        self.exit(self.exit_code)

    # ---- 狀態列 --------------------------------------------------------
    def _recount_context(self) -> None:
        """重算「這段對話下一輪會送出去多少 context」。

        **不能**用 step_finish 的 `tokens.input`:llama-server 的
        `prompt_eval_count` 在 prompt cache 命中時只算**新評估**的 token
        —— 實測一段 5k tokens 的對話,狀態列會顯示 43。這裡用的是壓縮門檻
        判斷的同一個估算(同一份 pruned payload),所以畫面上的數字與
        「什麼時候會自動壓縮」是同一把尺。

        只在回合結束與換 session 時算一次:它會走過整段歷史,不能綁在
        每 0.25 秒的狀態列 tick 上。
        """
        try:
            payload, _summary = self.engine.payload_messages()
            tokens, _chars = context_budget.estimate_tokens(
                messages=payload, tools=self.engine.openai_tools()
            )
        except Exception:  # noqa: BLE001 - 狀態列的一個數字不得變成新的失敗來源
            return
        self._context_tokens = int(tokens)

    def _compaction_mode(self) -> str:
        return str(getattr(self.compactor, "mode", "off"))

    def _project_instructions(self) -> bool:
        sections = getattr(self.engine.system_prompt, "sections", ())
        return any(section.name in ("project_agents", "lessons") for section in sections)

    def _refresh_status(self) -> None:
        try:
            bar = self.query_one("#status", Static)
        except Exception:  # noqa: BLE001 - 還沒 mount
            return
        parts = [
            self.engine.options.model,
            f"n_ctx={self.engine.options.n_ctx}",
            f"ctx≈{self._context_tokens}/{self.engine.options.n_ctx}",
            f"session={self.engine.session_id}",
            f"權限={self.engine.options.policy.name}",
            f"壓縮={self._compaction_mode()}",
            f"專案指示={'on' if self._project_instructions() else 'off'}",
            f"舊 reasoning={'送模' if self.keep_historical_reasoning else '不送'}",
        ]
        if self._turn_started is not None:
            self._spinner = (self._spinner + 1) % len(SPINNER_FRAMES)
            elapsed = time.monotonic() - self._turn_started
            parts.insert(
                0, f"{SPINNER_FRAMES[self._spinner]} {elapsed:.0f}s(Ctrl-C 中斷)"
            )
        self.status_text = " · ".join(parts)
        bar.update(Text(self.status_text))

    def _refresh_completions(self) -> None:
        prompt = self.query_one("#prompt", PromptInput)
        panel = self.query_one("#completions", Static)
        prompt.refresh_completions()
        if not prompt.completions:
            self.completion_text = ""
            panel.display = False
            panel.update(Text(""))
            return
        descriptions = dict(COMMANDS)
        lines = []
        for index, name in enumerate(prompt.completions):
            mark = "›" if index == prompt.completion_index else " "
            lines.append(f"{mark} {name:<11}{descriptions.get(name, '')}")
        self.completion_text = "\n".join(lines)
        panel.update(Text(self.completion_text))
        panel.display = True

    # ---- 輸入歷史 ------------------------------------------------------
    def _history_path(self) -> Path | None:
        if self.state_dir is None:
            return None
        return Path(self.state_dir) / HISTORY_FILENAME

    def _history_anchor(self, path: Path) -> Path:
        """dir-fd 要錨在**使用者環境**那一層,不是自己的 `sessions/<hash>`。

        ``client_paths`` 刻意允許 anchor 以上有 symlink(``/home`` 指到別處是正常的),
        anchor **以下**才是逐層 ``O_NOFOLLOW``。把 anchor 放在 `sessions/` 就只防到
        `<hash>/` 與檔名兩層:把 `codetrail/` 或 `sessions/` 換成 symlink 一樣穿得過去,
        於是這份逐字含 NDA 問題的檔會被寫到受管 state tree 之外。SessionStore 用的是
        ``state_home``,這裡跟它一致。
        """
        try:
            return client_store.state_home()
        except Exception:  # noqa: BLE001 - 沒有 HOME / XDG_STATE_HOME
            return path.parent.parent

    def _read_history(self) -> list[str]:
        """讀輸入歷史。走共用防線:path-based 讀會跟著 symlink 走,把別人的檔讀進來。"""
        path = self._history_path()
        if path is None:
            return []
        try:
            payload = client_paths.read_private_file(
                path.parent,
                path.name,
                _history_error,
                max_bytes=HISTORY_MAX_BYTES,
                anchor=self._history_anchor(path),
            )
        except Exception:  # noqa: BLE001 - 歷史讀不到不是啟動失敗
            return []
        if not payload:
            return []
        return decode_history(payload)[-HISTORY_MAX_LINES:]

    def _save_history(self) -> None:
        """把輸入歷史寫成 owner-only 的普通檔。

        這份檔逐字含使用者問過的問題 —— 對 NDA 專案而言那本身就是內容,所以跟
        session store 用同一組防線(dir fd 錨定、``O_NOFOLLOW``、``fstat`` 驗普通檔
        與 ``nlink == 1``、0600、原子替換)。
        """
        path = self._history_path()
        if path is None:
            return
        try:
            prompt = self.query_one("#prompt", PromptInput)
        except Exception:  # noqa: BLE001 - 畫面已經拆了
            return
        lines = [line for line in prompt.input_history if line.strip()][-HISTORY_MAX_LINES:]
        if not lines:
            return
        try:
            client_paths.replace_private_file(
                path.parent,
                path.name,
                encode_history(lines),
                _history_error,
                anchor=self._history_anchor(path),
            )
        except Exception:  # noqa: BLE001 - 歷史寫不了不是離開失敗
            pass


def encode_history(lines: Sequence[str]) -> bytes:
    """一行一則:每則用 JSON 字串包起來。

    直接 `"\\n".join(...)` 的話,一則多行的問題在讀回來時會變成好幾則獨立歷史
    (輸入框支援多行,所以那是正常輸入,不是邊角案例)。
    """
    body = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines)
    return (body + "\n").encode("utf-8")


def decode_history(payload: bytes) -> list[str]:
    """讀回 :func:`encode_history` 的內容。不是 JSON 的行當成舊格式的單行歷史。"""
    out: list[str] = []
    for raw in payload.decode("utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            out.append(raw)
            continue
        out.append(value if isinstance(value, str) else raw)
    return out


#: 「顯示 thinking」的初始值只來自 `client.json` 的 `show_reasoning`
#: (`CodeTrailApp(show_reasoning=...)`)。它只管畫面,與「舊回合 reasoning 要不要
#: 送模」是**兩個**設定:合併之後純 UI 操作就會改變模型看到的 context。
#: 以前這裡還認一個 `CODETRAIL_SHOW_REASONING`,那是第三個會漂移的來源。
