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
* **接續一段對話就要看得到它**。`/session`(選單或直接指定)與啟動時
  就接好的 Python 維護入口都重播**原始記錄**:文字、reasoning、工具呼叫
  (含未裁切的 `structuredContent`)與壓縮標記。畫面看不到、模型看得到的話,
  接下來每一則回答都在回應一段使用者看不見的脈絡。換不成功就 engine 與畫面
  **都不動**;回合進行中一律拒絕換。

`engine.send()` 跑在背景執行緒(協調器管),事件回到這裡才變成 widget;核准
在那個背景執行緒裡阻塞等 UI 回答。
"""
from __future__ import annotations

import json
import os
import shlex
import threading
import time
from time import monotonic as _progress_clock
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Collapsible, OptionList, Static, TextArea
from textual.widgets.option_list import Option

import client_config
import client_engine
import client_events
import client_paths
import client_review
import client_store
import client_turns
import command_allowlist
import context_budget

HISTORY_FILENAME = "tui_history"
HISTORY_MAX_LINES = 1000
HISTORY_MAX_BYTES = 4 * 1024 * 1024

#: 閒置時連按兩次 Ctrl-C 才離開,而且要在這段時間內。
DOUBLE_INTERRUPT_SECONDS = 2.0

# Only prefill details are sampled. Answer/reasoning/tool deltas stay immediate.
PROMPT_PROGRESS_INTERVAL_SECONDS = 10.0

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: 選單一次最多列幾段對話。
SESSION_PICKER_LIMIT = 50

#: 重播時,宣告了卻沒有結果的那一次呼叫。**不是**「已中斷」:那則結果是下一次
#: 送出前才由 engine 的 heal 補上去的,現在它真的還沒有結果。
PENDING_TOOL_STATUS = "pending"
PENDING_TOOL_OUTPUT = "這次呼叫沒有結果;下一題送出前會標成已中斷"
#: 找不到宣告它的呼叫(crash / 手改過的 session 檔)。顯示出來,不安靜丟掉。
ORPHAN_TOOL_NOTE = "(找不到宣告它的呼叫)"
#: 被標成 error 的 assistant 訊息:它不是答案,重播時要看得出來。
INCOMPLETE_ANSWER_NOTE = "上面這一則沒有完成(被截斷或出錯),不是答案。"

#: 斜線指令 → 一行說明。輸入框的補全與 `/help` 用的是**同一份**表:兩份會漂移。
COMMANDS: tuple[tuple[str, str], ...] = (
    ("/help", "這份說明"),
    ("/allow", "命令白名單:list / add <絕對目錄>"),
    ("/new", "開一個新對話"),
    ("/session", "選一個既有對話切換(/session <id> 直接指定)"),
    ("/compact", "立刻壓縮目前對話"),
    ("/review", "審查 HEAD 到工作目錄的淨變更，含新增檔案；不寫聊天歷史"),
    ("/queue", "待送訊息:list / add / edit / cancel / resume"),
    ("/supplement", "補充目前任務(/supplement <文字>,安全點才送入)"),
    ("/status", "目前模型、context、壓縮模式與 session 位置"),
    ("/tools", "本輪暴露的工具(裸名,依 tools/list 順序)"),
    ("/think", "切換主模型思考(/think on|off，預設 off)"),
    ("/exit", "離開"),
)

HELP_TAIL = (
    "其他輸入一律當成問題送給模型。\n"
    "Enter 送出、Alt+Enter 換行、↑/↓ 翻輸入歷史。\n"
    "忙碌時 Enter 選擇排到下一輪或補充目前任務;未送訊息用 /queue 查看。\n"
    "回合進行中 Ctrl-C 中斷整輪;閒置時連按兩次 Ctrl-C 或 Ctrl-D 離開。"
)

ALLOW_USAGE = "用法:/allow [list] | /allow add <絕對目錄>（含空白請加引號）"


def format_allow_list(settings: client_config.ClientSettings, policy: object) -> str:
    """列出本次讀取及檢查結果；runtime 狀態只認 MCP 已完成 handshake 的快照。"""
    valid_policy = (
        isinstance(policy, dict)
        and type(policy.get("schema")) is int
        and policy["schema"] == 1
        and all(type(policy.get(key)) is bool
                for key in ("run_command_enabled", "readonly", "use_container"))
        and all(isinstance(policy.get(key), list)
                and all(isinstance(value, str) and value.strip() for value in policy[key])
                for key in ("builtin_prefixes", "build_prefixes"))
    )
    lines: list[str] = []
    if valid_policy:
        lines.append(
            f"目前 MCP:run_command={'啟用' if policy['run_command_enabled'] else '停用'}；"
            f"readonly={'是' if policy['readonly'] else '否'}；"
            f"執行位置={'容器' if policy['use_container'] else '本機'}"
        )
        if policy["readonly"] or not policy["run_command_enabled"]:
            lines.append("目前 MCP 不允許執行 run_command。")
        if policy["use_container"]:
            lines.append("容器模式：目錄授權工具不可使用，不會改到本機執行。")
        for title, key in (("內建前綴", "builtin_prefixes"), ("已啟用 build 前綴", "build_prefixes")):
            lines.append(title + ":\n" + ("\n".join(f"  {item}" for item in policy[key]) or "  (沒有)"))
    else:
        lines.append("MCP 未回報目前 run_command 白名單；以下僅列已儲存授權。")

    names = "\n".join(f"  {name}" for name in settings.extra_allowed_commands)
    lines.append("額外命令（legacy／PATH）:\n" + (names or "  (沒有額外命令)"))
    inspection = command_allowlist.inspect_command_directories(
        settings.extra_allowed_command_dirs, extra_commands=settings.extra_allowed_commands,
    )
    lines.append("工具目錄（已儲存；本次檢查的授權工具）:")
    if not settings.extra_allowed_command_dirs:
        lines.append("  (沒有工具目錄)")
    commands_by_directory: dict[str, list[str]] = {}
    for name, path in sorted(inspection.commands.items()):
        commands_by_directory.setdefault(os.path.dirname(path), []).append(name)
    for directory in settings.extra_allowed_command_dirs:
        lines.append(f"  {directory}")
        names = commands_by_directory.get(directory, [])
        lines.extend(f"    {name}" for name in names)
        if not names:
            lines.append("    (沒有授權工具)")
        excluded = inspection.excluded.get(directory, {})
        if excluded:
            lines.append("    排除：" + "；".join(f"{reason} {count} 項" for reason, count in sorted(excluded.items())))
        if directory in inspection.errors:
            lines.append(f"    錯誤：{inspection.errors[directory]}")
    if inspection.errors:
        lines.append("授權解析失敗；目前所有 run_command 都將拒絕執行。")
    lines.extend((
        f"設定檔:{settings.path}" + ("" if settings.present else " (尚未建立)"),
        ALLOW_USAGE,
    ))
    return "\n".join(lines)


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


def local_time(stamp: Any) -> str:
    """本地時間。UTC 會讓「我昨天那一段」對不上使用者的時鐘。"""
    if not isinstance(stamp, (int, float)) or stamp <= 0:
        return "(時間不明)"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(stamp)))


def format_tool_output(message: Mapping[str, Any]) -> str:
    """一則 tool 訊息在展開區的完整輸出。

    送進模型的那一份 text 是套過 budget 的(``tool_result_adapter``);未裁切的
    核心留在 ``structuredContent``,而那份刻意只給 UI / eval。只顯示 text 的話,
    使用者在畫面上看到的是節錄,而純結構化結果的工具會顯示成「沒有輸出」。
    即時事件與重播走**同一個**函式:兩份格式化會漂移。
    """
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


# ============================================================
# 重播:session 記錄 → 畫面條目(純資料,不碰 Textual)
# ============================================================
@dataclass(frozen=True)
class HistoryEntry:
    """要貼回畫面的一則。"""

    kind: Literal[
        "user", "summary", "assistant", "assistant_error", "reasoning", "tool", "tool_orphan", "cancelled"
    ]
    text: str = ""
    tool: str = ""
    call_id: str = ""
    arguments: Mapping[str, Any] = field(default_factory=dict)
    status: str = ""
    output: str = ""
    structured: Any = None
    dropped: int = 0
    kept: int = 0


def _reasoning_text(message: Mapping[str, Any]) -> str:
    for key in context_budget.REASONING_FIELDS:
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _call_arguments(call: Mapping[str, Any]) -> dict[str, Any]:
    """wire 上的參數是 JSON **字串**;摘要行要的是解析過的 dict。

    解不出來的(串流被切一半)也要顯示原文:摘要行寫成 `list_dir()` 的話,
    使用者看到的是「呼叫了但沒有參數」,而真相是參數壞掉。
    """
    function = call.get("function")
    raw = function.get("arguments") if isinstance(function, Mapping) else None
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"arguments": raw}
    return parsed if isinstance(parsed, dict) else {"arguments": raw}


def history_entries(transcript: Sequence[Mapping[str, Any]]) -> list[HistoryEntry]:
    """把一段 transcript 攤成畫面條目。順序 = transcript 順序。

    **工具結果按「宣告群組」配對,不是按 id 反查整段歷史。** 沒有 server 給的 id
    時,fallback id 每個行程從 ``call_1`` 起算,所以同一段對話裡 `call_1` 會出現
    很多次;以 id 反查會把新結果貼到幾十輪之前的那個 block 上。規則:每則帶
    ``tool_calls`` 的 assistant 開一個新群組,結果只配**同群組**內還沒被回答的
    同 id;舊群組沒被回答的永遠是 pending(它真的沒有結果);配不到任何群組的
    結果是 orphan(顯示出來,不安靜丟掉)。壓縮標記也關掉群組:它之前的呼叫
    在模型眼中已經不存在了。
    """
    entries: list[HistoryEntry] = []
    open_calls: dict[str, int] = {}
    for record in transcript:
        if record.get("type") == "turn_cancelled":
            open_calls = {}
            entries.append(HistoryEntry(kind="cancelled", text="這一輪已取消；原始訊息保留。"))
            continue
        if record.get("type") == "compaction":
            open_calls = {}
            summary = record.get("summary")
            entries.append(
                HistoryEntry(
                    kind="summary",
                    text=summary if isinstance(summary, str) else "",
                    dropped=int(record.get("dropped") or 0),
                    kept=int(record.get("kept") or 0),
                )
            )
            continue
        role = record.get("role")
        if role == "user":
            content = record.get("content")
            if isinstance(content, str) and content:
                entries.append(HistoryEntry(kind="user", text=content))
            continue
        if role == "assistant":
            reasoning = _reasoning_text(record)
            if reasoning:
                entries.append(HistoryEntry(kind="reasoning", text=reasoning))
            content = record.get("content")
            if isinstance(content, str) and content:
                errored = record.get("tool_status") == client_events.STATUS_ERROR
                entries.append(
                    HistoryEntry(kind="assistant_error" if errored else "assistant", text=content)
                )
            calls = [call for call in (record.get("tool_calls") or ()) if isinstance(call, Mapping)]
            if calls:
                open_calls = {}
                for call in calls:
                    call_id = call.get("id")
                    call_id = call_id if isinstance(call_id, str) else ""
                    function = call.get("function")
                    name = function.get("name") if isinstance(function, Mapping) else ""
                    entries.append(
                        HistoryEntry(
                            kind="tool",
                            tool=name if isinstance(name, str) and name else "?",
                            call_id=call_id,
                            arguments=_call_arguments(call),
                            status=PENDING_TOOL_STATUS,
                            output=PENDING_TOOL_OUTPUT,
                        )
                    )
                    if call_id:
                        open_calls[call_id] = len(entries) - 1
            continue
        if role == "tool":
            call_id = record.get("tool_call_id")
            call_id = call_id if isinstance(call_id, str) else ""
            index = open_calls.pop(call_id, None) if call_id else None
            status = record.get("tool_status")
            status = status if isinstance(status, str) and status else client_events.STATUS_COMPLETED
            output = format_tool_output(record)
            structured = record.get("structured")
            if index is None:
                name = record.get("name")
                entries.append(
                    HistoryEntry(
                        kind="tool_orphan",
                        tool=name if isinstance(name, str) and name else "?",
                        call_id=call_id,
                        status=status,
                        output=output,
                        structured=structured,
                    )
                )
            else:
                entries[index] = replace(
                    entries[index], status=status, output=output, structured=structured
                )
    return entries


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
    """模型的 reasoning 本文，顯示由 client.json 的 show_reasoning 決定。"""

    def __init__(self) -> None:
        super().__init__(classes="entry reasoning")
        self._buffer = ""

    @property
    def text(self) -> str:
        return self._buffer

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


class SummaryBlock(Collapsible):
    """一次壓縮的標記:摘要收在裡面,計數在標題上。

    **壓縮前的原文仍然逐字留在它上面。** 這個標記只講「從這裡之後,模型看到的
    是摘要」—— 把 tail 再重繪一次的話,同一段對話在畫面上會出現兩次,而使用者
    分不出哪一次是真的發生過的。
    """

    def __init__(self, summary: str, *, dropped: int, kept: int) -> None:
        self._body = Static(Text(summary or "(這筆壓縮記錄裡沒有摘要正文)"), classes="tool-output")
        super().__init__(self._body, title="", collapsed=True, classes="entry summary")
        self.summary = summary
        self.dropped = int(dropped)
        self.kept = int(kept)
        self.title = (
            f"⇲ 壓縮摘要:先前 {self.dropped} 則已壓縮、"
            f"{self.kept} 則逐字保留(模型只看得到摘要)"
        )


# ============================================================
# 核准框
# ============================================================
class ApprovalScreen(ModalScreen[bool]):
    """核准一次工具呼叫。**Esc / 拒絕只拒絕這個工具**,不是中斷整輪。"""

    BINDINGS = [
        Binding("escape", "deny", "拒絕", show=False),
        Binding("y", "allow_key", "允許", show=False),
        Binding("n", "deny_key", "拒絕", show=False),
    ]

    def __init__(self, ticket: client_turns.ApprovalTicket, *, turn_id: str = "") -> None:
        super().__init__()
        self.ticket = ticket
        self.turn_id = turn_id
        self._editing_message = False

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
                yield Button("加入訊息", id="approval-message")
            with Vertical(id="approval-message-box"):
                yield TextArea(id="approval-message-text")
                with Horizontal(id="approval-message-actions"):
                    yield Button("排到下一輪", id="approval-queue")
                    yield Button("補充目前任務", id="approval-supplement")
            yield Static(
                Text("Esc 只拒絕這個工具,這一輪繼續;Ctrl-C 中斷整輪。", style="dim"),
                id="approval-hint",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button.id
        if button == "approval-message":
            self._editing_message = True
            self.query_one("#approval-message-box").display = True
            self.query_one("#approval-message-text", TextArea).focus()
            return
        if button in ("approval-queue", "approval-supplement"):
            editor = self.query_one("#approval-message-text", TextArea)
            try:
                self.app.coordinator.enqueue(
                    editor.text, mode="supplement" if button == "approval-supplement" else "queue",
                    session_id=self.ticket.request.session_id, turn_id=self.turn_id,
                )
            except client_turns.QueueError as exc:
                self.app._append(NoticeLine(str(exc)))
                return
            self.app.query_one("#prompt", PromptInput).remember(editor.text)
            editor.text = ""
            self.query_one("#approval-message-box").display = False
            self._editing_message = False
            self.query_one("#approval-deny", Button).focus()
            return
        if button in ("approval-allow", "approval-deny"):
            self.dismiss(button == "approval-allow")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Typing y/n in the message editor must never answer a tool approval.
        if action in ("allow_key", "deny_key"):
            return not self._editing_message
        return True

    def action_allow_key(self) -> None:
        self.action_allow()

    def action_deny_key(self) -> None:
        self.action_deny()

    def action_allow(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


# ============================================================
# 對話選單
# ============================================================
def session_row(info: Any, *, with_id: bool = False) -> str:
    """選單/清單的一列。大綱來自 :func:`client_store.session_outline`(零 LLM)。"""
    outline = str(getattr(info, "first_prompt", "") or "") or "(沒有問題內容)"
    head = f"{info.session_id}  " if with_id else ""
    return f"{head}{local_time(getattr(info, 'updated', 0.0))}  {getattr(info, 'turns', 0)} 輪  {outline}"


class SessionPickerScreen(ModalScreen[str | None]):
    """挑一段既有對話。Enter 選、Esc 取消。

    Esc 與 Ctrl-C 都**只收選單**:選單開著時沒有回合在跑(`/session` 進來之前
    已經擋過 busy),把它算成「中斷這一輪」或「離開」都是謊報。
    """

    BINDINGS = [Binding("escape", "cancel", "取消", show=False)]
    AUTO_FOCUS = "#picker-list"

    def __init__(self, sessions: Sequence[Any]) -> None:
        super().__init__()
        self.sessions = list(sessions)

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static(
                Text("選一個對話(↑/↓ 移動、Enter 接續、Esc 取消)", style="bold"), id="picker-title"
            )
            # 每一列都用 Text 包起來:對話的第一句話逐字來自使用者,裡面的
            # `[...]` 不得被當成 Textual 的 markup 解讀。
            yield OptionList(
                *(
                    Option(Text(session_row(info)), id=info.session_id)
                    for info in self.sessions
                ),
                id="picker-list",
            )

    def on_mount(self) -> None:
        listing = self.query_one("#picker-list", OptionList)
        listing.focus()
        if self.sessions:
            listing.highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option_id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReviewScreen(ModalScreen[bool]):
    """One scrollable, ephemeral report; raw model drafts never enter here."""

    BINDINGS = [Binding("escape", "close_review", "關閉審查", show=False)]

    def __init__(self) -> None:
        super().__init__()
        self.review_id = ""
        self.running = True
        self.close_when_done = False
        self.leave_when_done = False
        self.report = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="review"):
            yield Static(Text("工作區審查", style="bold"))
            yield Static(Text("HEAD 到目前工作目錄的淨變更，包含新增檔案。\n本次結果未寫入聊天歷史。"))
            yield Static(Text("準備審查…"), id="review-progress")
            with VerticalScroll(id="review-body"):
                yield Static(Text(""), id="review-report")
            yield Button("中斷並關閉", id="review-close", variant="primary")

    def on_mount(self) -> None:
        self.app._start_review(self)

    def accept_event(self, event: Mapping[str, Any]) -> None:
        if self.review_id and event.get("reviewID") != self.review_id:
            return
        if event.get("type") == "review_progress":
            self.query_one("#review-progress", Static).update(Text(str(event.get("message", ""))))
            return
        if not client_events.is_terminal_event(event):
            return
        self.running = False
        self.report = str(event.get("review_report", ""))
        state = str(event.get("review_state", "incomplete"))
        self.query_one("#review-progress", Static).update(Text(f"審查狀態：{state}"))
        self.query_one("#review-report", Static).update(Text(self.report))
        self.query_one("#review-close", Button).label = "關閉"
        if self.close_when_done:
            self.dismiss(self.leave_when_done)

    def request_close(self, *, leave: bool = False) -> None:
        self.leave_when_done = self.leave_when_done or leave
        if not self.running:
            self.dismiss(self.leave_when_done)
            return
        self.close_when_done = True
        self.app.coordinator.cancel(block=False, review_id=self.review_id)
        self.query_one("#review-progress", Static).update(Text("正在中斷審查並關閉唯讀 MCP…"))

    def action_close_review(self) -> None:
        self.request_close()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.request_close()

    def on_unmount(self) -> None:
        if self.running:
            self.app.coordinator.cancel(block=False, review_id=self.review_id)


class QueueChoiceScreen(ModalScreen[str | None]):
    """Choose an input's meaning explicitly, without stopping current work."""

    BINDINGS = [Binding("escape", "cancel", "保留草稿", show=False)]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="queue-choice"):
            yield Static(Text("這一輪還在跑,這則訊息要如何處理？", style="bold"))
            with VerticalScroll(id="queue-choice-body"):
                yield Static(Text(self.text))
            yield Button("排到下一輪", id="queue-next", variant="primary")
            yield Button("補充目前任務", id="queue-supplement")
            yield Static(Text("補充只在下一模型步驟前送入;本輪已收尾時保留到下一輪。Esc 保留草稿。"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss("supplement" if event.button.id == "queue-supplement" else "queue")

    def action_cancel(self) -> None:
        self.dismiss(None)


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
    #approval-message-box { display: none; height: 7; }
    #approval-message-text { height: 4; }
    #approval-message-actions { height: 3; }
    #picker {
        width: 90%; height: 80%; border: thick $primary; background: $surface; padding: 1 2;
    }
    #picker-list { height: 1fr; }
    #queue-choice {
        width: 85%; height: 80%; border: thick $primary; background: $surface; padding: 1 2;
    }
    #queue-choice-body { height: 1fr; }
    #queue-choice Button { margin-top: 1; width: 100%; }
    #review {
        width: 95%; height: 95%; border: thick $primary; background: $surface; padding: 1 2;
    }
    #review-progress { height: auto; max-height: 8; color: $text-muted; }
    #review-body { height: 1fr; border: round $primary-darken-2; padding: 0 1; }
    #review-report { height: auto; }
    #review-close { margin-top: 1; }
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
        # 啟動資訊只在 /status 查閱；初始對話區連 WARN 也不重播。
        self._startup_diagnostics = list(self.banner)
        self._user_interacted = False
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
            on_prime=self._prime_from_worker,
        )
        self._ui_thread_id = threading.get_ident()
        self._assistant: AssistantBlock | None = None
        self._reasoning: ReasoningBlock | None = None
        self._thinking_indicator: Static | None = None
        self._tools: dict[str, ToolBlock] = {}
        self._approval_screens: dict[str, ApprovalScreen] = {}
        self._review_screen: ReviewScreen | None = None
        self._queue_revisions: dict[str, int] = {}
        self._turn_started: float | None = None
        self._spinner = 0
        #: 本次模型請求收到幾段 reasoning、有沒有開始吐答案。preparing 只重置
        #: 狀態列相位,不刪掉已顯示的 widget 或 transcript。
        self._reasoning_chunks = 0
        self._answer_started = False
        self._compacting = False
        self._activity: dict[str, Any] | None = None
        self._activity_generating = False
        self._clear_prompt_progress()
        self._cancelling = False
        #: 最後一次由協調器跑完的預熱:``(時間, 觸發點, PrimeOutcome | None)``。不分是誰
        #: 排的(mount / new / session 由這裡排,compaction 由協調器自己排),全部經
        #: ``on_prime`` 回到這裡。只給 ``/status`` 看,不進對話區。
        self._last_prime: tuple[float, str, Any] | None = None
        self._last_interrupt = 0.0
        self._context_tokens: int | None = None
        self._context_count_generation = 0
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
        self._replay_startup_session()
        # 接續歷史先依完整計數檢查壓縮，再預熱下一輪的 prefix。
        self._prepare_idle("mount")
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
        if not self._user_interacted:
            self._startup_diagnostics.append(message)
            return
        self._append(NoticeLine(message))

    def _show_thinking(self) -> None:
        if self._thinking_indicator is None:
            self._thinking_indicator = Static(Text("思考中", style="bold red"), classes="thinking-indicator")
            self._append(self._thinking_indicator)

    def _clear_thinking(self) -> None:
        if self._thinking_indicator is not None:
            self._thinking_indicator.display = False
            self._thinking_indicator.remove()
            self._thinking_indicator = None

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

    # ---- 重播 ----------------------------------------------------------
    def _entry_widgets(self, entries: Sequence[HistoryEntry]) -> list[Widget]:
        """條目 → widget。**重播出來的 ToolBlock 不登記進 ``_tools``**:那張表是給
        即時事件用的(以 call id 當 key),塞進重播的 block 之後,新的一次呼叫會
        更新到上一段對話的那個 block 上,而畫面上看起來像是它自己動了。
        """
        widgets: list[Widget] = []
        for entry in entries:
            if entry.kind == "user":
                widgets.append(UserMessage(entry.text))
            elif entry.kind == "cancelled":
                widgets.append(ErrorLine(entry.text))
            elif entry.kind == "summary":
                widgets.append(SummaryBlock(entry.text, dropped=entry.dropped, kept=entry.kept))
            elif entry.kind == "reasoning":
                block = ReasoningBlock()
                block.append(entry.text)
                block.display = self.show_reasoning
                widgets.append(block)
            elif entry.kind in ("assistant", "assistant_error"):
                block = AssistantBlock()
                block.finish(entry.text)
                widgets.append(block)
                if entry.kind == "assistant_error":
                    widgets.append(ErrorLine(INCOMPLETE_ANSWER_NOTE))
            elif entry.kind in ("tool", "tool_orphan"):
                block = ToolBlock(entry.tool or "?", entry.arguments, entry.status or "?")
                block.set_output(entry.output)
                if entry.kind == "tool_orphan":
                    block.title = f"{block.title} {ORPHAN_TOOL_NOTE}"
                widgets.append(block)
        return widgets

    def _mount_history(self, widgets: Sequence[Widget], *, clear: bool) -> None:
        log = self.query_one("#log", VerticalScroll)
        if clear:
            log.remove_children()
        if widgets:
            log.mount(*widgets)
        log.scroll_end(animate=False)

    def _replay_history(
        self, transcript: Sequence[Mapping[str, Any]], *, clear: bool
    ) -> list[HistoryEntry]:
        entries = history_entries(transcript)
        self._mount_history(self._entry_widgets(entries), clear=clear)
        return entries

    def _resumed_notice(self, snapshot: Any, entries: Sequence[HistoryEntry]) -> str:
        return (
            f"已接續 {getattr(snapshot, 'session_id', '')}"
            f"(畫面 {len(entries)} 則、模型歷史 {len(getattr(snapshot, 'messages', ()))} 則、"
            f"壓縮 {int(getattr(snapshot, 'compactions', 0) or 0)} 次)"
        )

    def _replay_startup_session(self) -> None:
        """Python 維護入口選定 session：engine 在 app 建起來之前就接續好了。

        重播只掛在 `/session` 上的話,這一條路仍然會是空白畫面 —— 使用者
        看不到自己上次問過什麼,但模型接得下去。
        """
        snapshot = getattr(self.engine, "resumed_snapshot", None)
        if snapshot is None or getattr(snapshot, "session_id", None) != self.engine.session_id:
            return
        try:
            entries = self._replay_history(getattr(snapshot, "transcript", ()), clear=False)
        except Exception as exc:  # noqa: BLE001 - 顯示不出來不得變成啟動失敗
            self._append(ErrorLine(f"這段對話顯示不出來:{type(exc).__name__}: {exc}"))
            return
        self._append(NoticeLine(self._resumed_notice(snapshot, entries)))

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

    def _prime_from_worker(self, reason: str, outcome: Any) -> None:
        """協調器的 ``on_prime``:每一次預熱(含壓縮後那一次)跑完都從預熱執行緒到這裡。"""
        self._from_worker(self._note_prime, reason, outcome)

    # ---- 事件 ----------------------------------------------------------
    def handle_event(self, event: Mapping[str, Any]) -> None:
        if event.get("reviewID") is not None:
            if self._review_screen is not None:
                self._review_screen.accept_event(event)
            self._refresh_status()
            return
        kind = event.get("type")
        if kind == client_events.TYPE_QUEUE:
            self._on_queue_event(event)
            return
        if kind == client_events.TYPE_ACTIVITY:
            self._on_activity(event)
            return
        if kind == client_events.TYPE_TEXT_DELTA:
            self._clear_thinking()
            self._answer_started = True
            self._mark_generating()
            self._ensure_assistant().append(str(client_events.event_part(event).get("text", "")))
            self.query_one("#log", VerticalScroll).scroll_end(animate=False)
            return
        if kind == client_events.TYPE_TEXT:
            self._clear_thinking()
            self._answer_started = True
            self._mark_generating()
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
            self._append_notice(str(event.get("message", "")))
            return
        if kind == client_events.TYPE_ERROR:
            self._turn_started = None
            self._reset_phase()
            self._append(ErrorLine(str(event.get("message", ""))))
            self._refresh_status()
            return
        if kind == client_events.TYPE_STEP_FINISH:
            self._on_step_finish(event)
            return

    def _on_queue_event(self, event: Mapping[str, Any]) -> None:
        if event.get("sessionID") != self.engine.session_id:
            return
        part = client_events.event_part(event)
        message_id = str(part.get("id", ""))
        revision = part.get("revision")
        if not isinstance(revision, int) or revision <= self._queue_revisions.get(message_id, 0):
            return
        self._queue_revisions[message_id] = revision
        # The coordinator keeps only a bounded receipt history; so does the UI.
        retained = {item.id for item in self.coordinator.queue_snapshot()}
        self._queue_revisions = {key: value for key, value in self._queue_revisions.items() if key in retained}
        status = str(part.get("status", ""))
        if status == "delivered":
            self._append(UserMessage(str(part.get("text", ""))))
            self._assistant = None
            self._reasoning = None
        elif status == "delivering" and part.get("id"):
            self._turn_started = time.monotonic()
            self._reset_phase()
        labels = {"waiting": "等待送入", "deferred": "待下一輪", "delivering": "接收中",
                  "delivered": "已送達歷史", "cancelled": "已取消"}
        mode = "補充" if part.get("mode") == "supplement" else "排隊"
        self._append(NoticeLine(
            f"{message_id} {mode}:{labels.get(status, status)}。{part.get('reason', '')}"))
        self._refresh_status()

    def _on_activity(self, event: Mapping[str, Any]) -> None:
        """只收本輪的短暫活動;它不進對話區,也不重播到別段 session。"""
        if (
            event.get("sessionID") != self.engine.session_id
            or self._turn_started is None
            or not self.coordinator.busy
            or self.coordinator.cancelled
        ):
            return
        part = client_events.event_part(event)
        operation, phase = part.get("operation"), part.get("phase")
        if operation not in ("response", "compact") or phase not in (
            "preparing", "waiting_model", "waiting_response", "prompt_processing",
            "generating", "tool", "approval", "validating", "persisting",
        ):
            return
        if phase in ("validating", "persisting") and operation != "compact":
            return
        if phase == "preparing":
            self._reset_phase(compacting=operation == "compact")
        elif phase == "prompt_processing" and self._activity_generating:
            # 同一請求開始生成後,晚到的 prefill 進度不得蓋掉答案或摘要階段。
            return
        if phase in ("generating", "validating", "persisting", "tool", "approval"):
            self._activity_generating = True
        if operation == "compact" or phase in ("tool", "approval"):
            self._clear_thinking()
        activity: dict[str, Any] = {"operation": operation, "phase": phase}
        percent = part.get("percent")
        if phase == "prompt_processing":
            now = _progress_clock()
            if self._prompt_started is None:
                self._prompt_started = now
            self._prompt_received = now
            progress = client_events.prompt_progress_snapshot(part.get("progress"))
            if progress is not None:
                activity["progress"] = progress
                # Compute from the same validated snapshot used for the counts.
                activity["percent"] = 100 * progress["processed"] // progress["total"]
            elif "progress" not in part and type(percent) is int and 0 <= percent <= 100:
                activity["percent"] = percent
            if "percent" not in activity:
                # Invalid data must not leave the last trustworthy numbers visible.
                self._prompt_display = None
                self._prompt_sampled = None
        else:
            self._clear_prompt_progress()
        tool = part.get("tool")
        if phase in ("tool", "approval") and isinstance(tool, str) and tool:
            activity["tool"] = tool
        self._activity = activity
        self._refresh_status()

    def _mark_generating(self) -> None:
        changed = self._prompt_started is not None or (
            self._activity is not None and self._activity["phase"] != "generating"
        )
        self._activity_generating = True
        self._clear_prompt_progress()
        if self._activity is not None:
            self._activity = {
                "operation": self._activity["operation"], "phase": "generating",
            }
        if changed:
            self._refresh_status()

    def _on_tool_event(self, event: Mapping[str, Any]) -> None:
        self._clear_thinking()
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
        """即時事件那條路:從 engine 剛記下的那一則 tool 訊息取完整輸出。

        格式化本身走 :func:`format_tool_output`(重播用的是同一個),兩份會漂移。
        """
        for message in reversed(self.engine.messages):
            if message.get("role") != "tool" or message.get("tool_call_id") != call_id:
                continue
            return format_tool_output(message)
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
        self._reset_phase()
        self._refresh_status()

    def _on_reasoning(self, token: str) -> None:
        if not token:
            return
        self._reasoning_chunks += 1
        self._mark_generating()
        self._show_thinking()
        self._ensure_reasoning().append(token)
        self.query_one("#log", VerticalScroll).scroll_end(animate=False)

    # ---- 核准 ----------------------------------------------------------
    def _show_approval(self, ticket: client_turns.ApprovalTicket) -> None:
        screen = ApprovalScreen(ticket, turn_id=self.coordinator.turn_id)
        self._approval_screens[ticket.approval_id] = screen

        def _answered(granted: bool | None) -> None:
            self._approval_screens.pop(ticket.approval_id, None)
            # A timeout/cancel may close approval while its message draft is
            # still being edited. Preserve that unsent text without sending it.
            try:
                draft = screen.query_one("#approval-message-text", TextArea).text
                if draft.strip():
                    prompt = self.query_one("#prompt", PromptInput)
                    if not prompt.text.strip():
                        prompt.text = draft
                    else:
                        prompt.remember(draft)
                        self._append(NoticeLine("核准框中的未送草稿已保留在輸入歷史。"))
            except Exception:  # noqa: BLE001 - the screen may already be unmounted
                pass
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
            if text.split(maxsplit=1)[0].lower() not in ("/queue", "/supplement"):
                self._consume_prompt(prompt, message.text)
                self._command(text)
            elif self._command(text):
                self._consume_prompt(prompt, message.text)
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
        self._user_interacted = True
        try:
            self.coordinator.start_turn(text)
        except client_turns.TurnCoordinator.Busy:
            if self.coordinator.reviewing:
                self._append(NoticeLine("工作區審查進行中；輸入保留在草稿，可用 /queue add 保留到聊天。"))
                return False
            self._choose_message_mode(text)
            self._refresh_completions()
            return False
        except Exception as exc:  # noqa: BLE001 - 送不出去不得帶走 UI
            self._append(ErrorLine(f"{type(exc).__name__}: {exc}"))
            self._refresh_completions()
            return False
        self._append(UserMessage(text))
        self._turn_started = time.monotonic()
        self._reset_phase()
        self._refresh_status()
        self._refresh_completions()
        return True

    def _choose_message_mode(self, text: str) -> None:
        target, turn_id = self.engine.session_id, self.coordinator.turn_id

        def _chosen(mode: str | None) -> None:
            if mode is None:
                return
            try:
                self.coordinator.enqueue(text, mode=mode, session_id=target, turn_id=turn_id)
            except (client_turns.QueueError, client_turns.TurnCoordinator.Busy) as exc:
                self._append(NoticeLine(str(exc)))
                return
            prompt = self.query_one("#prompt", PromptInput)
            if prompt.text.strip() == text:
                self._consume_prompt(prompt, prompt.text)
            else:
                prompt.remember(text)
            self._refresh_completions()

        self.push_screen(QueueChoiceScreen(text), _chosen)

    # ---- 指令 ----------------------------------------------------------
    def _command(self, line: str) -> bool:
        self._user_interacted = True
        name, _, argument = line[1:].partition(" ")
        name = name.strip().lower()
        argument = argument.strip()
        handler = getattr(self, f"_cmd_{name}", None)
        if handler is None:
            self._append(NoticeLine(f"未知指令 /{name};/help 看清單。"))
            return True
        return handler(argument) is not False

    def _cmd_queue(self, argument: str) -> bool:
        action, _, rest = argument.partition(" ")
        action = action or "list"
        try:
            if action == "list":
                entries = self.coordinator.queue_snapshot()
                lines = [f"{item.id} [{item.status}] {item.mode} session={item.session_id} "
                         f"turn={item.turn_id or '-'}\n{item.text}\n{item.reason}" for item in entries]
                state = "暫停; /queue resume 繼續" if self.coordinator.queue_paused else "依序等待"
                self._append(NoticeLine(f"待送佇列:{state}\n" + ("\n\n".join(lines) or "(沒有項目)")))
            elif action == "add":
                self.coordinator.enqueue(rest, mode="queue")
            elif action == "edit":
                message_id, _, text = rest.partition(" ")
                self.coordinator.edit_queued(message_id, text)
            elif action == "cancel":
                self.coordinator.cancel_queued(rest.strip())
            elif action == "resume":
                if not self.coordinator.resume_queue():
                    self._append(NoticeLine("沒有可繼續的待送訊息。"))
            else:
                raise client_turns.QueueError("用法:/queue list | add <文字> | edit <id> <文字> | cancel <id> | resume")
        except (client_turns.QueueError, client_turns.TurnCoordinator.Busy) as exc:
            self._append(NoticeLine(str(exc) or "回合忙碌中,請等本輪結束再 resume。"))
            return False
        return True

    def _cmd_supplement(self, argument: str) -> bool:
        try:
            self.coordinator.enqueue(argument, mode="supplement")
        except client_turns.QueueError as exc:
            self._append(NoticeLine(str(exc)))
            return False
        return True

    def _cmd_help(self, _argument: str) -> None:
        lines = [f"  {name:<11}{description}" for name, description in COMMANDS]
        self._append(NoticeLine("指令:\n" + "\n".join(lines) + "\n" + HELP_TAIL))

    def _cmd_allow(self, argument: str) -> None:
        """本地新增工具目錄；MCP 每次執行重新讀取授權，聊天 policy 保持原樣。"""
        try:
            parts = shlex.split(argument)
        except ValueError:
            self._append(ErrorLine(ALLOW_USAGE))
            return
        action = parts[0] if parts else "list"
        directories = parts[1:]
        if (
            action not in ("list", "add")
            or (action == "list" and directories)
            or (action == "add" and len(directories) != 1)
        ):
            self._append(ErrorLine(ALLOW_USAGE))
            return
        if action != "list":
            if self.engine.options.policy.name == "readonly":
                self._append(ErrorLine("唯讀模式只允許 /allow list，不能修改命令白名單。"))
                return
            # 佇列中有待送訊息不影響設定;只擋目前正在進行的工作。
            if (
                self.coordinator.busy
                or self.coordinator.pending_approvals()
                or self.coordinator.reviewing
            ):
                self._append(ErrorLine(
                    "回合、核准或審查進行中，不能修改命令白名單；仍可用 /allow list 查看。"
                ))
                return
        try:
            if action == "list":
                settings = client_config.load_client_settings()
                status = ""
            else:
                settings, changed = client_config.add_allowed_command_directory(directories[0])
                if changed:
                    status = "已加入；目前 session 後續命令立即生效。"
                else:
                    status = "未變更：目錄已在清單中，未寫入設定檔。"
            # 純快取 property；不可讀 engine.tool_specs 的舊快照，也不可呼叫會
            # start() 的 mcp.tools()，list 必須在死亡／重啟中的 MCP 上仍然零副作用。
            policy = getattr(getattr(self.engine, "mcp", None), "command_policy", {})
            listing = format_allow_list(settings, policy)
        except (client_config.ClientConfigError, OSError, ValueError) as exc:
            self._append(ErrorLine(f"/allow: {exc}"))
            return
        self._append(NoticeLine((status + "\n" if status else "") + listing))

    def _cmd_exit(self, _argument: str) -> None:
        if self._review_screen is not None:
            self._review_screen.request_close(leave=True)
            return
        if self._busy_notice("/exit"):
            return
        self._leave()

    _cmd_quit = _cmd_exit

    def _cmd_think(self, argument: str) -> None:
        value = argument.lower()
        if value not in ("", "on", "off"):
            self._append(NoticeLine("用法:/think [on|off]；不帶參數切換。"))
            return
        if self._busy_notice("/think"):
            return
        if not self.engine.thinking_supported:
            self._append(NoticeLine("此模型尚未偵測到 thinking 控制支援；請重新執行 set_config 偵測。"))
            return
        enabled = not self.engine.options.thinking if not value else value == "on"
        try:
            changed = self.engine.set_thinking(enabled)
        except Exception as exc:  # noqa: BLE001 - 中止預熱失敗時保留原模式
            self._append(ErrorLine(f"無法切換 thinking:{exc}"))
            return
        if changed:
            self._last_prime = None
            self._recount_context()
            self._prime("think")
        self._append(NoticeLine(f"think={'on' if self.engine.options.thinking else 'off'}"))
        self._refresh_status()

    def _cmd_tools(self, _argument: str) -> None:
        lines = []
        for index, spec in enumerate(self.engine.tool_specs.values(), start=1):
            flag = "ro" if spec.read_only else "rw"
            lines.append(f"  {index:2d}. [{flag}] {spec.name}")
        self._append(NoticeLine("\n".join(lines) or "(沒有工具)"))

    def _busy_notice(self, what: str) -> bool:
        """回合進行中就擋下這個指令並回 True。

        ``/new`` 與 ``/session`` 直接換掉 ``engine.session_id`` 與 ``messages``:
        在回合中做等於把還沒寫完的答案與自動壓縮落到**另一段**對話,舊對話留下
        一則沒有回答的 user,新對話多出一則沒有相鄰 user 的 assistant。
        排隊 worker 也會開始新回合,所以閒置時仍須檢查 pending 佇列:
        有待送項目就不准切换;沒有項目時背景也沒有可啟動的下一輪。
        """
        if self.coordinator.busy:
            self._append(NoticeLine(f"這一輪還在跑,{what} 要等它結束;Ctrl-C 可以中斷它。"))
            return True
        try:
            self.coordinator.assert_session_change_allowed()
        except (client_turns.QueueError, client_turns.TurnCoordinator.Busy) as exc:
            self._append(NoticeLine(f"{what}: {exc}"))
            return True
        return False

    def _cmd_new(self, _argument: str) -> None:
        if self._busy_notice("/new"):
            return
        try:
            self.engine.new_session()
        except Exception as exc:  # noqa: BLE001 - 建不了新 session 就留在原地
            self._append(ErrorLine(f"無法開新對話:{exc}(仍在 {self.engine.session_id})"))
            return
        self.coordinator.session_changed()
        self._queue_revisions.clear()
        # 新對話的 prefix 只剩 system 段:server 那邊對它多半是冷的,先送出去。
        self._prime("new")
        # 工具 block 以 call id 當 key;換了對話,舊 id 不得再被新呼叫接上。
        self._tools.clear()
        self._assistant = None
        self._reasoning = None
        self._turn_started = None
        self._reset_phase()
        # 畫面也要換過去:上一段對話留在畫面上的話,新對話的第一個回答會接在
        # 別段對話的下面,而模型完全看不到那一段。
        self._mount_history((), clear=True)
        self._append(NoticeLine(f"新對話:{self.engine.session_id}"))
        self._recount_context()
        self._refresh_status()

    def _sessions(self, limit: int) -> list[Any] | None:
        try:
            return list(self.engine.store.list_sessions(limit=limit))
        except Exception as exc:  # noqa: BLE001 - 讀不到清單不得帶走 UI
            self._append(ErrorLine(f"讀不到既有對話:{type(exc).__name__}: {exc}"))
            return None

    def _cmd_session(self, argument: str) -> None:
        """`/session <id>` 直接換;`/session` 開選單。"""
        if argument:
            self._switch_session(argument, what="/session")
            return
        if self._busy_notice("/session"):
            return
        sessions = self._sessions(SESSION_PICKER_LIMIT)
        if sessions is None:
            return
        if not sessions:
            self._append(NoticeLine("這個專案還沒有已保存的對話。"))
            return
        self.push_screen(SessionPickerScreen(sessions), self._picked_session)

    def _picked_session(self, session_id: str | None) -> None:
        # 取消(Esc / Ctrl-C / 收掉畫面)一律是「什麼都沒發生」。
        if not session_id:
            return
        self._switch_session(session_id, what="/session")

    def _switch_session(self, session_id: str, *, what: str = "/session") -> None:
        """換到另一段既有對話:engine 與畫面**要嘛一起換,要嘛都不動**。

        順序是契約:先一次受信讀取(`load_session`,零狀態改動)、再把 widget 建
        好、最後才 `adopt` 並換畫面。反過來的話,讀壞掉的 session 檔會留下「engine
        已經換過去、畫面還是上一段」的狀態 —— 使用者對著舊畫面問下一題,而那一題
        會被寫進另一段對話。
        """
        if self._busy_notice(what):
            return
        try:
            snapshot = self.engine.load_session(session_id)
            entries = history_entries(getattr(snapshot, "transcript", ()))
            widgets = self._entry_widgets(entries)
        except Exception as exc:  # noqa: BLE001 - 到這裡為止 engine / 畫面都還沒動
            self._append(ErrorLine(f"無法接續:{exc}"))
            return
        self.engine.adopt(snapshot)
        self.coordinator.session_changed()
        self._queue_revisions.clear()
        # 工具 block 以 call id 當 key;換了對話,舊 id 不得再被新呼叫接上。
        self._tools.clear()
        self._assistant = None
        self._reasoning = None
        self._turn_started = None
        self._reset_phase()
        self._mount_history(widgets, clear=True)
        self._append(NoticeLine(self._resumed_notice(snapshot, entries)))
        self._prepare_idle("session")
        self._recount_context()
        self._refresh_status()

    def _cmd_compact(self, _argument: str) -> None:
        try:
            self.coordinator.start_compaction()
        except client_turns.TurnCoordinator.Busy:
            self._append(NoticeLine("這一輪還在跑;Ctrl-C 可以中斷它。"))
            return
        except client_turns.QueueError as exc:
            self._append(NoticeLine(str(exc)))
            return
        self._turn_started = time.monotonic()
        self._reset_phase(compacting=True)
        self._refresh_status()

    def _cmd_review(self, argument: str) -> None:
        if argument:
            self._append(NoticeLine("/review 不接受參數；範圍是目前工作目錄的淨變更。"))
            return
        if self._busy_notice("/review"):
            return
        if self._review_screen is not None:
            return
        screen = ReviewScreen()
        self._review_screen = screen
        self.push_screen(screen, self._review_closed)

    def _start_review(self, screen: ReviewScreen) -> None:
        try:
            job = client_review.ReviewJob(self.engine)
            screen.review_id = self.coordinator.start_review(job)
        except Exception as exc:
            screen.running = False
            screen.report = f"審查未啟動：{type(exc).__name__}: {exc}"
            screen.query_one("#review-progress", Static).update(Text(screen.report))
            screen.query_one("#review-close", Button).label = "關閉"
        self._refresh_status()

    def _review_closed(self, leave: bool) -> None:
        self._review_screen = None
        self._refresh_status()
        self.query_one("#prompt", PromptInput).focus()
        if leave:
            self._leave()

    def _cmd_status(self, _argument: str) -> None:
        path = self.engine.store.path(self.engine.session_id) if self.engine.session_id else None
        lines = [
            f"model={self.engine.options.model}",
            f"n_ctx={self.engine.options.n_ctx} max_output={self.engine.options.max_output_tokens}",
            f"tools={len(self.engine.tool_specs)} permission={self.engine.options.policy.name}",
            f"壓縮模式={self._compaction_mode()}",
            f"session={self.engine.session_id or '(尚未建立)'}",
            f"session 檔={path if path else '(不落檔)' if self.engine.session_id else '(尚未建立)'}",
            f"think={'on' if self.engine.options.thinking else 'off'}",
            f"專案指示={'已載入' if self._project_instructions() else '未載入'}",
            f"舊回合 reasoning={'送模' if self.keep_historical_reasoning else '不進模型'}",
            f"prompt cache 預熱={self._prime_status()}",
            f"待送訊息={len(self.coordinator.queue_snapshot(pending_only=True))}"
            f"({'暫停, /queue resume' if self.coordinator.queue_paused else '依序等待'})",
        ]
        if self.engine.store_error:
            lines.append(
                f"⚠ session 落檔失敗({self.engine.store_error});這段對話只在記憶體裡。"
            )
        if self._startup_diagnostics:
            lines.extend(("啟動診斷:", *self._startup_diagnostics))
        self._append(NoticeLine("\n".join(lines)))

    # ---- 鍵盤動作 ------------------------------------------------------
    def _close_picker(self) -> bool:
        """對話選單開著就收掉它並回 True。

        選單不是一個回合、也不是一個核准:Ctrl-C 收它不算「中斷這一輪」
        (那是謊報),Ctrl-D 收它也不算「離開」。
        """
        screen = self.screen
        if not isinstance(screen, SessionPickerScreen):
            return False
        try:
            screen.dismiss(None)
        except Exception:  # noqa: BLE001 - 畫面已經不在了
            pass
        return True

    def action_interrupt(self) -> None:
        """Ctrl-C。選單開著 = 只收選單;回合進行中 = 中斷整輪;閒置 = 連按兩次離開。"""
        if self._review_screen is not None:
            if not self._review_screen.running:
                self._review_screen.request_close()
            elif self.coordinator.cancel(block=False, review_id=self._review_screen.review_id):
                self._review_screen.query_one("#review-progress", Static).update(Text("正在中斷審查…"))
            return
        if self._close_picker():
            return
        if isinstance(self.screen, QueueChoiceScreen):
            self.screen.dismiss(None)
        # block=False:MCP 取消要等寬限期(10 秒)+ SIGTERM + 重新 spawn。
        # 同步跑在這裡就是整個畫面凍住,而且 worker 送事件用的
        # call_from_thread 也會排在後面一起卡住。
        if self.coordinator.cancel(block=False):
            self._reset_phase()
            self._cancelling = True
            self._refresh_status()
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
        * 對話選單開著:只收選單(沒有選任何一段,也沒有離開)。
        * 回合進行中:不離開。直接拆掉畫面會留下一個卡在核准上的 worker,
          而 `command_chat` 隨即關掉共用的 MCP。要停就先 Ctrl-C 中斷。
        * 閒置:存歷史然後離開。
        """
        if self._review_screen is not None:
            self._review_screen.request_close(leave=True)
            return
        if isinstance(self.screen, ApprovalScreen):
            self.screen.action_deny()
            return
        if isinstance(self.screen, QueueChoiceScreen):
            self.screen.dismiss(None)
            return
        if self._close_picker():
            return
        if self._busy_notice("Ctrl-D"):
            return
        self._leave()

    def _leave(self) -> None:
        """唯一的離開出口:先把輸入歷史落檔再退出。

        `App.on_unmount` 靠不住 —— Textual 拆畫面時子 widget 已經先移除,
        `query_one("#prompt")` 會失敗,於是正常退出的路徑一個 byte 都沒寫。
        """
        if self._busy_notice("離開"):
            return
        self._save_history()
        self.exit(self.exit_code)

    # ---- 預熱 ----------------------------------------------------------
    def _prime(self, reason: str) -> None:
        """把「下一輪的 prefix」丟到背景先送一次。

        engine 沒有這個能力(舊 engine / 替身)或回合進行中的話,協調器直接回
        False,這裡就是 no-op。准入(policy、模型鎖、server 忙不忙、context gate)
        全在 engine 那端;這個介面只決定**什麼時候**值得排一次:歷史剛換過、
        server 那邊的 prompt cache 對新的 prefix 多半是冷的那幾個時刻。

        預熱不是一則對話內容:它不進對話區、不進事件流,只在 ``/status`` 與
        狀態列看得到。結果不在這裡接:協調器跑完的**每一次**預熱(含壓縮後它自己排的
        那一次)都經 ``on_prime`` 回到 :meth:`_note_prime`。
        """
        self.coordinator.prime_in_background(reason)

    def _prepare_idle(self, reason: str) -> None:
        """原始畫面已重播後，先檢查自動壓縮，再預熱可用的歷史。"""
        try:
            self.coordinator.prepare_idle(reason)
        except (client_turns.TurnCoordinator.Busy, client_turns.QueueError):
            return
        if self.coordinator.busy:
            self._turn_started = time.monotonic()
            self._reset_phase(compacting=True)

    def _note_prime(self, reason: str, outcome: Any) -> None:
        self._last_prime = (time.time(), reason, outcome)

    def _prime_status(self) -> str:
        """``/status`` 的那一行:最近一次預熱的結果、原因、時間與觸發點。

        跳過的時候要說得出**為什麼**:只寫「沒有預熱」的話,使用者看到的是
        「第一題還是很慢」,而分不出是預熱沒發生、還是預熱沒有用。``觸發=`` 講的是
        這一次是哪一種入口(mount / new / session / compaction):壓縮後那一次不回到
        這裡的話,這一行會停在 mount 那一次的 ``sent``,而下一題面對的其實是冷的 cache。
        """
        if self._last_prime is None:
            return "尚未"
        stamp, reason, outcome = self._last_prime
        when = time.strftime("%H:%M:%S", time.localtime(stamp))
        trigger = f"觸發={reason or '?'}"
        if outcome is None:
            # engine 的契約是不 raise;真的丟出來就不得顯示成「送出了」。
            return f"error {when} {trigger}"
        if getattr(outcome, "sent", False):
            return f"sent {when} {trigger}"
        return f"skipped({getattr(outcome, 'reason', '') or '?'}) {when} {trigger}"

    # ---- 狀態列 --------------------------------------------------------
    def _recount_context(self) -> None:
        """背景計算完整 next-turn prefix；不阻塞 UI、不採用本次重算量。"""
        self._context_count_generation += 1
        generation = self._context_count_generation
        self._context_tokens = None
        counter = getattr(self.engine, "context_tokens", None)
        if not callable(counter):
            return
        identity = (self.engine.session_id, id(self.engine.messages), len(self.engine.messages))

        def recount() -> None:
            try:
                value = counter()
                if type(value) is not int or value < 0:
                    value = None
            except Exception:  # noqa: BLE001 - a display-only count can remain unknown
                value = None
            self._from_worker(self._accept_context_count, generation, identity, value)

        threading.Thread(target=recount, name="codetrail-context-count", daemon=True).start()

    def _accept_context_count(self, generation: int, identity: tuple, value: int | None) -> None:
        current = (self.engine.session_id, id(self.engine.messages), len(self.engine.messages))
        if generation != self._context_count_generation or identity != current:
            return
        self._context_tokens = value
        self._refresh_status()

    def _compaction_mode(self) -> str:
        return str(getattr(self.compactor, "mode", "off"))

    def _project_instructions(self) -> bool:
        sections = getattr(self.engine.system_prompt, "sections", ())
        return any(section.name in ("project_agents", "lessons") for section in sections)

    def _reset_phase(self, *, compacting: bool = False) -> None:
        self._clear_thinking()
        self._reasoning_chunks = 0
        self._answer_started = False
        self._compacting = compacting
        self._activity = None
        self._activity_generating = False
        self._clear_prompt_progress()
        self._cancelling = False

    def _clear_prompt_progress(self) -> None:
        self._prompt_started: float | None = None
        self._prompt_received: float | None = None
        self._prompt_sampled: float | None = None
        self._prompt_display: dict[str, Any] | None = None

    def _prompt_phase(self) -> str:
        """Sample the latest request's SSE counts, without doing I/O on the UI loop."""
        now = _progress_clock()
        if self._prompt_started is None or now - self._prompt_started < PROMPT_PROGRESS_INTERVAL_SECONDS:
            return "等待回應"
        if self._prompt_sampled is None or now - self._prompt_sampled >= PROMPT_PROGRESS_INTERVAL_SECONDS:
            self._prompt_display = dict(self._activity or {})
            self._prompt_sampled = now
        snapshot = self._prompt_display or {}
        label = "prompt processing"
        if "percent" in snapshot:
            label += f"({snapshot['percent']}%)"
        progress = snapshot.get("progress")
        if progress is not None:
            processed, total = progress["processed"], progress["total"]
            label += f" · {processed:,}/{total:,} tok"
            cache = progress.get("cache")
            if cache is not None:
                label += f" · cache {cache:,}"
            # The initial snapshot precedes the first decode batch. Its elapsed
            # time is not a zero-speed measurement of the batch still running.
            if cache is not None and processed > cache:
                elapsed_ms = progress.get("time_ms", 0)
                if elapsed_ms > 0:
                    rate = (processed - cache) * 1000 / elapsed_ms
                    label += f" · {rate:,.1f} tok/s · prefill {elapsed_ms / 1000:.0f}s"
                if self._prompt_received is not None:
                    age = now - self._prompt_received
                    if age >= PROMPT_PROGRESS_INTERVAL_SECONDS:
                        label += f" · 距更新 {age:.0f}s"
        return label

    def _turn_phase(self) -> str:
        """這一輪目前在哪一段。

        只顯示收到的活動與文字/reasoning;沒有可靠進度就不猜百分比。
        """
        if self._cancelling:
            return "中斷中"
        if self._activity is not None:
            operation = self._activity["operation"]
            phase = self._activity["phase"]
            labels = {
                "preparing": "準備請求",
                "waiting_model": "等待模型",
                "waiting_response": "等待回應",
                "prompt_processing": "prompt processing",
                "generating": "產生摘要中" if operation == "compact" else "產生回應中",
                "tool": "執行工具",
                "approval": "等待核准",
                "validating": "驗證摘要中",
                "persisting": "儲存摘要中",
            }
            label = self._prompt_phase() if phase == "prompt_processing" else labels[phase]
            if "tool" in self._activity:
                label += f" {self._activity['tool']}"
            if operation == "compact":
                return f"compact · {label}"
            if phase != "generating" or not (self._answer_started or self._reasoning_chunks):
                return label
        if self._compacting:
            return "壓縮中"
        if self._answer_started:
            return "回答中"
        if self._reasoning_chunks:
            return f"thinking {self._reasoning_chunks} 段"
        return "等待首個 token"

    def _refresh_status(self) -> None:
        try:
            bar = self.query_one("#status", Static)
        except Exception:  # noqa: BLE001 - 還沒 mount
            return
        parts = [
            self.engine.options.model,
            f"n_ctx={self.engine.options.n_ctx}",
            f"ctx={self._context_tokens if self._context_tokens is not None else '?'}/{self.engine.options.n_ctx}",
            f"session={self.engine.session_id or '(尚未建立)'}",
            f"壓縮={self._compaction_mode()}",
            f"think={'on' if self.engine.options.thinking else 'off'}",
        ]
        active_status = ""
        if self._turn_started is not None:
            self._spinner = (self._spinner + 1) % len(SPINNER_FRAMES)
            elapsed = time.monotonic() - self._turn_started
            parts.insert(
                0, f"{SPINNER_FRAMES[self._spinner]} {elapsed:.0f}s(Ctrl-C 中斷)"
            )
            parts.insert(1, self._turn_phase())
            active_status = " · ".join(parts[:2])
        if getattr(self.engine, "priming", False):
            # 預熱握著模型鎖:這時候送出的下一題會在鎖上等它送完,狀態列要講得出來。
            parts.append("prompt cache 預熱中")
        pending = self.coordinator.queue_snapshot(pending_only=True)
        if pending:
            parts.append(f"待送={len(pending)}" + ("(暫停,/queue resume)" if self.coordinator.queue_paused else ""))
        self.status_text = " · ".join(parts)
        # Keep the measured counts/rate visible at normal SSH terminal widths.
        # Return to the original single row as soon as this prefill ends.
        rows = 1
        if self._prompt_display is not None and self._turn_started is not None:
            width = max(1, (bar.size.width or self.size.width) - 2)
            rows = min(3, max(1, (Text(active_status).cell_len + width - 1) // width))
        bar.styles.height = rows
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
