#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_theme — `aicode` 介面主題的單一註冊點（呈現層，零 I/O）。

主題名稱的合法集合只有一份：`client_config.THEME_VALUES`（client.json 的 `theme` 鍵）。
這裡的 :data:`THEMES` 必須與它逐一相同，模組載入時就核對 —— 設定接受的名字沒有外觀、
或外觀存在卻存不進設定，都是「我選了但沒生效」的無聲失敗。

主題**只改呈現**：顏色、版面、裝飾符號。訊息內容、事件、session 在每個主題都一樣；
裝飾符號是 :class:`ThemeGlyph`，它不參與任何選取。複製結果只有工具卡標題列會隨主題不同
（Textual 的三擊全選本來就會帶到 Collapsible 的標題，default 是「▶ ✓ …」、codex 是
「• Called …」）。

Textual 8.2.5 才有 ``Theme(ansi=…)``、終端原生色的自動切換與 App 上的 ``-theme-<name>``
class；requirements 允許 ``textual>=8,<9``，8.0–8.2.4 仍要能用。那些版本的 Theme 不帶 ansi
參數（見 :data:`NATIVE_ANSI_THEMES`），class 與 ``App.ansi_color`` 由 CodeTrailApp.watch_theme
補上，codex 一樣用終端自己的顏色。

codex 主題的外觀參照 openai/codex rust-v0.155.1 的 codex-rs/tui（Apache-2.0）：styles.md
的配色規範，以及 history_cell／status_indicator_widget／bottom_pane 的畫法與 snapshot。
這裡只用 Textual 重寫樣式與版面，不含 Codex 的程式碼。刻意不做的：終端背景色探測
（OSC 10/11）與使用者訊息／輸入框底色 —— Codex 自己在取不到終端背景時也不上色，這裡固定
走那條路；狀態標題的 RGB 漸層同理（配色未知時 Codex 用靜態 dim）。

新增主題：`client_config.THEME_VALUES` 加名字、:data:`THEMES` 加一筆 :class:`ThemeSpec`
（ANSI 主題的 variables 必須有 ansi-background／ansi-foreground，否則執行中切過去 Textual
會因 Screen 的 CSS 變數未定義而整個崩潰）、`.-theme-<名字>` 規則只寫在 :data:`THEME_CSS`
（寫在 widget 的 DEFAULT_CSS 會被 Textual scope 到該 widget 子樹，永遠不匹配 App 上的 class）。
"""
from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType

from rich.text import Text
from textual.selection import Selection
from textual.theme import BUILTIN_THEMES, Theme
from textual.widgets import Static

import client_config

#: 版面行為。classic = 原本的版面；codex = 狀態列拆成活動列＋footer、工具卡 Codex 標題、
#: 補全在輸入框下方。新主題沿用其中一種即可。
CHROME_CLASSIC = "classic"
CHROME_CODEX = "codex"
CHROMES = (CHROME_CLASSIC, CHROME_CODEX)

#: ThemeGlyph 的角色：使用者訊息、助理回答、工具輸出樹枝、輸入框提示符。
GLYPH_ROLES = ("user", "assistant", "tree", "composer")

#: ThemeGlyph 固定佔的欄寬（Codex 的 LIVE_PREFIX_COLS）。
GLYPH_CELLS = 2

#: ANSI 主題必須自己定義的變數：Screen 的 DEFAULT_CSS 引用它們，Textual 只有在非 ANSI
#: 主題才會補。少了它們，執行中切換主題會 UnresolvedVariableError。
ANSI_REQUIRED_VARIABLES = ("ansi-background", "ansi-foreground")

#: 這一版 Textual 的 Theme 認不認得 ``ansi``（8.2.5 起才有）。不認得時直接傳會在 import 就
#: TypeError —— 整個 aicode 起不來，連 default 主題的使用者也一樣。
NATIVE_ANSI_THEMES = "ansi" in inspect.signature(Theme).parameters


@dataclass(frozen=True)
class ThemeSpec:
    """一個介面主題。顏色交給 Textual Theme，版面與裝飾由 chrome／glyphs 決定。"""

    name: str
    label: str
    #: 主題選單上的一句說明。
    description: str
    textual: Theme
    #: 用終端自己的 ANSI 顏色（不轉成 RGB）。新版 Textual 由 Theme.ansi 負責，舊版由
    #: App.ansi_color 補；兩條路都以這個欄位為準。
    ansi: bool
    chrome: str
    #: 角色 → (文字, Rich style)。文字 "" = 這個主題不顯示該角色；非空時恰好 2 欄寬。
    glyphs: Mapping[str, tuple[str, str]]
    #: 工具卡的 (收合, 展開) symbol。
    tool_symbols: tuple[str, str]
    #: 輸入框空白時的提示字（只顯示，不會送出）。
    placeholder: str


_NO_GLYPHS = MappingProxyType({role: ("", "") for role in GLYPH_ROLES})


def _textual_theme(*, ansi: bool, **kwargs: object) -> Theme:
    """建 Textual Theme；只有認得 ``ansi`` 的 Textual 才傳它（舊版由 App.ansi_color 補）。"""
    if NATIVE_ANSI_THEMES:
        kwargs["ansi"] = ansi
    return Theme(**kwargs)


_DEFAULT = ThemeSpec(
    name="default",
    label="default",
    description="原本的 CodeTrail 外觀",
    # 與 Textual 內建 textual-dark 同色：default 主題的畫面必須與加入主題前逐一相同。
    textual=replace(BUILTIN_THEMES["textual-dark"], name="default"),
    ansi=False,
    chrome=CHROME_CLASSIC,
    glyphs=_NO_GLYPHS,
    tool_symbols=("▶", "▼"),
    placeholder="",
)

# Codex styles.md：前景／背景一律用終端自己的預設色；只用 ANSI cyan（提示、選取、狀態）、
# green（成功）、red（錯誤）、magenta（Codex），避開 blue 與 black／white 前景。
_CODEX = ThemeSpec(
    name="codex",
    label="codex",
    description="仿 Codex CLI：終端原生配色、› 與 • 標記、狀態列在輸入框上方",
    textual=_textual_theme(
        name="codex",
        ansi=True,
        dark=True,
        primary="ansi_cyan",
        secondary="ansi_magenta",
        accent="ansi_cyan",
        warning="ansi_yellow",
        error="ansi_red",
        success="ansi_green",
        foreground="ansi_default",
        background="ansi_default",
        surface="ansi_default",
        panel="ansi_default",
        boost="ansi_default",
        variables={
            # 與 Textual 內建 ansi-dark 相同；見 ANSI_REQUIRED_VARIABLES。
            "ansi-background": "ansi_black",
            "ansi-foreground": "ansi_white",
            # 選中列：cyan bold、不上底色（Codex accent_style）。
            "block-cursor-foreground": "ansi_cyan",
            "block-cursor-background": "ansi_default",
            "block-cursor-text-style": "bold",
            "block-cursor-blurred-foreground": "ansi_default",
            "block-cursor-blurred-background": "ansi_default",
            "block-cursor-blurred-text-style": "none",
            "block-hover-background": "ansi_default",
            "border": "ansi_cyan",
            "border-blurred": "ansi_default",
            "scrollbar": "ansi_bright_black",
            "scrollbar-hover": "ansi_white",
            "scrollbar-active": "ansi_cyan",
            "scrollbar-background": "ansi_default",
            "scrollbar-background-hover": "ansi_default",
            "scrollbar-background-active": "ansi_default",
            "scrollbar-corner-color": "ansi_default",
            "link-color": "ansi_cyan",
            "link-style": "underline",
            "link-color-hover": "ansi_cyan",
            "link-background-hover": "ansi_default",
            "link-style-hover": "bold underline",
            # markdown_render.rs MarkdownStyles::default。
            "markdown-h1-color": "ansi_default",
            "markdown-h1-text-style": "bold underline",
            "markdown-h2-color": "ansi_default",
            "markdown-h2-text-style": "bold",
            "markdown-h3-color": "ansi_default",
            "markdown-h3-text-style": "bold italic",
            "markdown-h4-color": "ansi_default",
            "markdown-h4-text-style": "italic",
            "markdown-h5-color": "ansi_default",
            "markdown-h5-text-style": "italic",
            "markdown-h6-color": "ansi_default",
            "markdown-h6-text-style": "italic",
        },
    ),
    ansi=True,
    chrome=CHROME_CODEX,
    glyphs=MappingProxyType({
        "user": ("› ", "bold dim"),
        "assistant": ("• ", "dim"),
        "tree": ("└ ", "dim"),
        "composer": ("› ", "bold"),
    }),
    tool_symbols=("•", "•"),
    placeholder="向 CodeTrail 提問，或輸入 / 使用指令",
)

THEMES: Mapping[str, ThemeSpec] = MappingProxyType({spec.name: spec for spec in (_DEFAULT, _CODEX)})


def _check_registry() -> None:
    """設定與外觀是同一份名單；不一致就讓 aicode 啟動失敗，而不是某個主題無聲失效。"""
    if tuple(THEMES) != tuple(client_config.THEME_VALUES):
        raise RuntimeError(
            f"client_theme.THEMES {tuple(THEMES)} 與 client_config.THEME_VALUES "
            f"{tuple(client_config.THEME_VALUES)} 不一致"
        )
    for name, spec in THEMES.items():
        if spec.name != name or spec.textual.name != name:
            raise RuntimeError(f"主題 {name!r} 的名稱與 Textual 主題名稱不一致")
        if spec.chrome not in CHROMES:
            raise RuntimeError(f"主題 {name!r} 的 chrome {spec.chrome!r} 不在 {CHROMES}")
        if set(spec.glyphs) != set(GLYPH_ROLES):
            raise RuntimeError(f"主題 {name!r} 的 glyph 角色必須正好是 {GLYPH_ROLES}")
        for role, (text, _style) in spec.glyphs.items():
            if text and Text(text).cell_len != GLYPH_CELLS:
                raise RuntimeError(f"主題 {name!r} 的 {role} glyph 必須恰好 {GLYPH_CELLS} 欄寬")
        if NATIVE_ANSI_THEMES and spec.textual.ansi is not spec.ansi:
            raise RuntimeError(f"主題 {name!r} 的 ansi 與 Textual 主題不一致")
        if spec.ansi:
            missing = [key for key in ANSI_REQUIRED_VARIABLES if key not in spec.textual.variables]
            if missing:
                raise RuntimeError(f"ANSI 主題 {name!r} 缺少變數 {missing}")


_check_registry()


def spec_for(name: str) -> ThemeSpec:
    """目前 Textual 主題名 → ThemeSpec。不在註冊表（非 aicode 的 App）一律當 default。"""
    return THEMES.get(name) or THEMES[client_config.DEFAULT_THEME]


# ============================================================
# 主題 CSS（App 層）
# ============================================================
#: 所有 `.-theme-<name>` 規則只寫在這裡，由 CodeTrailApp.CSS 串接。App 層 CSS 永遠壓過
#: widget 的 DEFAULT_CSS（Textual 的 specificity 第一位就是「是不是 DEFAULT_CSS」），
#: 所以這裡可以直接覆寫 Collapsible／TextArea／Button 在 :ansi 下的預設。
THEME_CSS = """
/* ---- codex：history cells（Codex 的 cell 從第 0 欄開始，cell 之間一個空白行）---- */
.-theme-codex #log { padding: 0 1 0 0; }
.-theme-codex .entry.user { color: $foreground; text-style: none; padding: 1 0; }
.-theme-codex .entry.notice { color: $foreground; }
.-theme-codex .entry.error { color: $error; }
.-theme-codex .entry.reasoning { color: $foreground; text-style: dim italic; padding: 0 0 0 2; }
.-theme-codex .tool-output { color: $foreground; text-style: dim; }
.-theme-codex MarkdownBlock > .code_inline { color: $primary; background: $background; }
.-theme-codex MarkdownBlockQuote { color: $success; background: $background; border-left: outer $success; }
/* markdown_render.rs：標題靠左、區塊之間一個空白行，回答第一行就接在「• 」後面。 */
.-theme-codex MarkdownHeader { margin: 1 0 1 0; content-align: left middle; }
.-theme-codex AssistantBlock Markdown > MarkdownBlock:first-child { margin-top: 0; }
.-theme-codex ToolBlock, .-theme-codex SummaryBlock {
    background: $background; border-top: none; padding: 0; margin: 0 0 1 0;
}
.-theme-codex ToolBlock > CollapsibleTitle, .-theme-codex SummaryBlock > CollapsibleTitle {
    padding: 0; color: $foreground; text-style: none;
}
.-theme-codex ToolBlock.-status-completed > CollapsibleTitle { color: $success; }
.-theme-codex ToolBlock.-status-error > CollapsibleTitle,
.-theme-codex ToolBlock.-status-denied > CollapsibleTitle { color: $error; }
.-theme-codex SummaryBlock > CollapsibleTitle { text-style: dim; }
.-theme-codex ToolBlock > Contents, .-theme-codex SummaryBlock > Contents { padding: 0 0 0 2; }

/* ---- codex：bottom pane ---- */
.-theme-codex #activity.-active { display: block; }
.-theme-codex #activity { color: $foreground; padding: 0; }
.-theme-codex #composer { padding: 1 0; }
.-theme-codex #prompt { border: none; padding: 0; background: $background; }
.-theme-codex #prompt:focus { border: none; }
.-theme-codex #prompt > .text-area--cursor-line { background: $background; }
.-theme-codex #prompt > .text-area--placeholder { color: $foreground; text-style: dim; }
.-theme-codex #status { background: $background; color: $foreground; text-style: dim; padding: 0 2; }
.-theme-codex #completions { dock: bottom; color: $foreground; padding: 0; }
.-theme-codex Screen.-completions-open #status { display: none; }

/* ---- codex：清單與核准畫在底部（Codex bottom pane），不透出底下的對話 ---- */
.-theme-codex ApprovalScreen, .-theme-codex SessionPickerScreen, .-theme-codex ReviewScreen,
.-theme-codex QueueChoiceScreen, .-theme-codex ThemePickerScreen { align: left bottom; }
.-theme-codex #approval, .-theme-codex #picker, .-theme-codex #review,
.-theme-codex #queue-choice, .-theme-codex #theme-picker {
    width: 100%; max-width: 100%; border: none; background: $background; padding: 1 2;
}
.-theme-codex #approval-body, .-theme-codex #review-body { border: none; padding: 0; }
.-theme-codex #approval-hint, .-theme-codex #review-progress, .-theme-codex #theme-picker-hint {
    color: $foreground; text-style: dim;
}
.-theme-codex #picker-list, .-theme-codex #theme-picker-list { border: none; background: $background; padding: 0; }
.-theme-codex ModalScreen Button {
    border: none; height: 1; min-width: 0; padding: 0 1; background: $background;
    color: $foreground; text-style: none;
}
.-theme-codex ModalScreen Button:hover { background: $background; color: $accent; }
.-theme-codex ModalScreen Button:focus { background: $background; color: $accent; text-style: bold; }
"""


# ============================================================
# 裝飾符號
# ============================================================
class ThemeGlyph(Static):
    """Codex 式的 2 欄前綴（「› 」「• 」「└ 」）。純裝飾，不是內容。

    * dock 在父容器左邊，所以父容器其餘子項整段往右縮 2 欄 —— 等於 Codex 的「首行符號、
      續行縮排」。
    * **不參與選取**：ALLOW_SELECT=False 擋不住 Textual 的全選（三擊會把整棵子樹、含隱藏
      與不可選的 widget 都放進 selections），所以 get_selection 一律回 None。否則三擊或手動
      複製鍵會帶出符號，default 主題下也會多出空行。
    * 文字與顯示只由目前主題決定：mount 時自己讀 ``app.theme``，之後由 App 的 watch_theme
      統一呼叫 :meth:`apply`。
    """

    ALLOW_SELECT = False
    DEFAULT_CSS = """
    ThemeGlyph {
        dock: left;
        width: 2;
        height: 1;
        display: none;
    }
    """

    def __init__(self, role: str) -> None:
        if role not in GLYPH_ROLES:
            raise ValueError(f"未知的 glyph 角色 {role!r}")
        super().__init__("", classes=f"glyph-{role}")
        self.role = role
        #: 目前畫著的 (文字, style)。初值就是 DEFAULT_CSS 的隱藏狀態：沒有符號的主題
        #: （default）mount 與重播時不必逐一重畫幾千個空 widget。
        self._applied: tuple[str, str] = ("", "")

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        return None

    def apply(self, spec: ThemeSpec) -> None:
        glyph = spec.glyphs.get(self.role, ("", ""))
        if glyph == self._applied:
            return
        self._applied = glyph
        text, style = glyph
        self.update(Text(text, style=style))
        self.display = bool(text)

    def on_mount(self) -> None:
        self.apply(spec_for(self.app.theme))


# ============================================================
# codex 版面的文字
# ============================================================
def format_elapsed_compact(seconds: float) -> str:
    """Codex status_indicator_widget::fmt_elapsed_compact：0s、59s、1m 00s、1h 00m 00s。"""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {total % 3600 // 60:02d}m {total % 60:02d}s"


def activity_bullet(elapsed: float) -> Text:
    """Codex motion.rs 非 truecolor 的活動符號：「•」與 dim「◦」每 600ms 交替。

    樣式寫成 span 而不是 Text 的 base style：後面接上的字不能跟著變 dim。
    """
    bullet = Text()
    if int(max(0.0, elapsed) * 1000) // 600 % 2 == 0:
        bullet.append("•")
    else:
        bullet.append("◦", style="dim")
    return bullet


def activity_line(phase: str, elapsed: float) -> Text:
    """「• <階段> (<時間> • Ctrl-C 中斷)」。

    Codex 在配色未知時標題是靜態 dim、按鍵名稱不 dim；階段文字（含 prefill 詳情）照
    CodeTrail 原本的中文，不翻成 Codex 的英文。
    """
    line = activity_bullet(elapsed)
    line.append(" ")
    line.append(phase, style="dim")
    line.append(f" ({format_elapsed_compact(elapsed)} • ", style="dim")
    line.append("Ctrl-C")
    line.append(" 中斷)", style="dim")
    return line


#: 工具狀態 → Codex 式動詞。完成與失敗都是 Called（Codex 只用 • 的顏色區分），但失敗另外
#: 寫出狀態字 —— NO_COLOR 或單色終端下只剩文字可以分辨。
_TOOL_VERBS = {"completed": "Called", "error": "Called", "denied": "Denied", "pending": "Calling"}
#: 動詞已經講清楚狀態、不必再加「→ 狀態」的那些。
_TOOL_STATUS_IN_VERB = frozenset({"completed", "denied"})


def codex_tool_title(tool: str, inner: str, status: str, *, orphan_note: str = "") -> Text:
    """Codex history_cell/mcp.rs 的標題：「Called tool(args)」（• 由工具卡的 symbol 顯示）。

    ``inner`` 是與 default 標題同一份的參數摘要；動詞 bold、工具 cyan、參數 dim。顏色明確
    寫成 default，只有 symbol 吃工具卡依狀態上的色。
    """
    title = Text()
    title.append(_TOOL_VERBS.get(status, "Called"), style="bold default")
    title.append(" ")
    title.append(tool, style="cyan")
    title.append(f"({inner})", style="dim default")
    if status not in _TOOL_STATUS_IN_VERB:
        title.append(f" → {status}", style="dim default")
    if orphan_note:
        title.append(f" {orphan_note}", style="dim default")
    return title


def codex_completion_lines(rows: Sequence[tuple[str, str]], selected: int | None) -> Text:
    """補全面板：選中整列 cyan bold，其餘指令預設色、說明 dim。

    ``rows`` 是 (前綴＋補齊寬度的指令欄, 說明)；逐列相接後的純文字與 classic 的
    completion_text 完全相同 —— 主題只改樣式，不改內容。
    """
    text = Text()
    for index, (head, description) in enumerate(rows):
        if index:
            text.append("\n")
        if index == selected:
            text.append(head + description, style="bold cyan")
            continue
        text.append(head)
        text.append(description, style="dim")
    return text
