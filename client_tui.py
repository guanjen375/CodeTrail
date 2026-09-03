#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_tui — 終端前端(stdlib readline REPL + 串流輸出)。

刻意只用標準函式庫:這個工具的部署路徑是「一台離線機器 + 一份 requirements」,
多一個 TUI 相依就多一個離線環境裝不起來的理由。

畫面上的三個保證:
  * 串流。模型吐字就印,不等整輪結束。
  * 核准框**完整顯示參數**(含整份 patch)。截斷過的核准等於沒有核准。
  * Ctrl-C 只中斷這一輪:HTTP 串流會被關掉(llama-server 放掉 slot)、
    進行中的 MCP 呼叫會送出取消通知,REPL 回到提示符。
"""
from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import client_engine
import client_events
import client_mcp
import client_paths
import client_store

PROMPT = "\n\033[1;36myou>\033[0m "
HISTORY_FILENAME = "tui_history"
HISTORY_MAX_LINES = 1000
HISTORY_MAX_BYTES = 4 * 1024 * 1024

SHOW_REASONING_ENV = "CODETRAIL_SHOW_REASONING"

HELP = """\
指令:
  /help              這份說明
  /exit  /quit       離開
  /new               開一個新對話
  /sessions          列出這個專案的既有對話
  /resume <id>       接續一個既有對話
  /compact           立刻壓縮目前對話
  /status            目前模型、context、壓縮模式與 session 位置
  /tools             本輪暴露的工具(裸名,依 tools/list 順序)
  /thinking          切換是否顯示模型的 thinking

其他輸入一律當成問題送給模型。Ctrl-C 中斷這一輪,Ctrl-D 離開。
"""


def _supports_colour(stream: Any) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("TERM") != "dumb"


class _HistoryError(RuntimeError):
    """history 檔的位置或權限不合契約。呼叫端一律 fail-open(只是少一份歷史)。"""


def _history_error(message: str) -> _HistoryError:
    return _HistoryError(message)


class Tui:
    def __init__(
        self,
        engine: client_engine.Engine,
        *,
        banner: tuple[str, ...] = (),
        state_dir: Path | None = None,
        compact: Callable[[], str] | None = None,
        status: Callable[[], tuple[str, ...]] | None = None,
        on_idle: Callable[[], str] | None = None,
        before_send: Callable[[], str] | None = None,
        on_session_change: Callable[[], None] | None = None,
    ) -> None:
        self.engine = engine
        self.banner = banner
        self.state_dir = state_dir
        self.compact = compact
        self.status = status
        # 壓縮的觸發點:助理已經完整答完、沒有進行中的請求。這是搬進 Python
        # 之後唯一剩下的觸發條件 —— 中間沒有另一個行程能插進來。
        self.on_idle = on_idle
        #: 送出**之前**跑一次,回非空字串就印出來。停用警告走這裡:等
        #: `on_idle` 才講的話,這一輪先撞 context gate 就完全看不到原因。
        self.before_send = before_send
        #: `/new` / `/resume` 之後呼叫:只做重綁(例如壓縮器換 session),**不**
        #: 消耗 before_send「每個 session 只講一次」的那一次——否則 resume 時
        #: 講的那句被丟掉,真正送出時就不再講了。
        self.on_session_change = on_session_change
        self.show_reasoning = str(os.environ.get(SHOW_REASONING_ENV, "")).strip() in ("1", "true", "yes")
        self._colour = _supports_colour(sys.stdout)
        self._streamed = False

    # ---- readline ------------------------------------------------------
    def _history_path(self) -> Path | None:
        if self.state_dir is None:
            return None
        return self.state_dir / HISTORY_FILENAME

    def _setup_readline(self) -> Any:
        try:
            import readline
        except ImportError:  # pragma: no cover - platform without readline
            return None
        path = self._history_path()
        if path is not None:
            # 讀取端也走共用防線:path-based `readline.read_history_file()` 會跟著
            # symlink 走,把別人的檔讀進歷史。
            try:
                payload = client_paths.read_private_file(
                    path.parent, path.name, _history_error,
                    max_bytes=HISTORY_MAX_BYTES, anchor=path.parent.parent,
                )
            except Exception:  # noqa: BLE001 - 歷史讀不到不是啟動失敗
                payload = None
            if payload:
                for line in payload.decode("utf-8", errors="replace").splitlines()[-HISTORY_MAX_LINES:]:
                    if line.strip():
                        readline.add_history(line)
            readline.set_history_length(HISTORY_MAX_LINES)
        return readline

    def _save_history(self, readline: Any) -> None:
        """把輸入歷史寫成 owner-only 的普通檔。

        這份檔逐字含使用者問過的問題 —— 對 NDA 專案而言那本身就是內容。所以它
        跟 session store 用同一組防線(`client_paths.replace_private_file`:dir fd
        錨定、`O_NOFOLLOW`、`fstat` 驗普通檔與 nlink==1、0600、原子替換)。
        **不用** `readline.write_history_file()`:那是 path-based,而且要先
        `O_TRUNC` 開檔才驗得到 hard link——被指向的別人的檔案在拒絕之前就先被清空了。
        """
        path = self._history_path()
        if readline is None or path is None:
            return
        try:
            count = readline.get_current_history_length()
            items = [readline.get_history_item(index) for index in range(1, count + 1)]
            lines = [item for item in items if isinstance(item, str) and item.strip()]
            payload = ("\n".join(lines[-HISTORY_MAX_LINES:]) + "\n").encode("utf-8")
            client_paths.replace_private_file(
                path.parent, path.name, payload, _history_error, anchor=path.parent.parent
            )
        except Exception:  # noqa: BLE001 - 歷史寫不了不是離開失敗
            pass

    # ---- output --------------------------------------------------------
    def _dim(self, text: str) -> str:
        return f"\033[2m{text}\033[0m" if self._colour else text

    def _note(self, text: str) -> None:
        print(self._dim(f"[codetrail] {text}"), flush=True)

    def _on_text(self, token: str) -> None:
        if not self._streamed:
            self._streamed = True
        sys.stdout.write(token)
        sys.stdout.flush()

    def _on_reasoning(self, token: str) -> None:
        if not self.show_reasoning:
            return
        sys.stdout.write(self._dim(token))
        sys.stdout.flush()

    def _on_event(self, event: dict[str, Any]) -> None:
        if event.get("type") != client_events.TYPE_TOOL_USE:
            return
        part = event.get("part") or {}
        state = part.get("state") or {}
        tool = part.get("tool", "?")
        status = state.get("status", "?")
        arguments = state.get("input") or {}
        summary = ", ".join(f"{k}={_short(v)}" for k, v in sorted(arguments.items()))
        print()
        self._note(f"{tool}({summary}) → {status}")

    # ---- approval ------------------------------------------------------
    def approve(self, request: client_engine.ApprovalRequest) -> bool:
        print()
        print(self._dim("─" * 60))
        print("需要核准:")
        print(request.render())
        print(self._dim("─" * 60))
        try:
            answer = input("核准這次呼叫? [y/N] ").strip().lower()
        except EOFError:
            return False
        return answer in ("y", "yes")

    # ---- loop ----------------------------------------------------------
    def run(self) -> int:
        readline = self._setup_readline()
        for line in self.banner:
            print(line)
        print(self._dim("輸入 /help 看指令。"))
        try:
            while True:
                try:
                    raw = input(PROMPT if self._colour else "\nyou> ")
                except EOFError:
                    print()
                    return 0
                except KeyboardInterrupt:
                    print()
                    continue
                text = raw.strip()
                if not text:
                    continue
                if text.startswith("/"):
                    if self._command(text) is False:
                        return 0
                    continue
                self._turn(text)
        finally:
            self._save_history(readline)

    def _turn(self, text: str) -> None:
        self._streamed = False
        if self.before_send is not None:
            try:
                notice = self.before_send()
            except Exception as exc:  # noqa: BLE001 - 警告失敗不得擋住送出
                notice = f"壓縮狀態檢查失敗:{type(exc).__name__}: {exc}"
            if notice:
                self._note(notice)
        print()
        try:
            result = self.engine.send(
                text,
                on_event=self._on_event,
                on_text=self._on_text,
                on_reasoning=self._on_reasoning,
                approve=self.approve,
            )
        except (KeyboardInterrupt, client_engine.TurnCancelled):
            print()
            self._note("已中斷這一輪。")
            return
        except client_mcp.McpCallCancelledError:
            print()
            self._note("工具呼叫已取消。")
            return
        except Exception as exc:  # noqa: BLE001 - REPL 不因單一輪失敗而退出
            print()
            self._note(f"這一輪失敗: {type(exc).__name__}: {exc}")
            return
        if self._streamed:
            print()
        for notice in result.notices:
            self._note(notice)
        # 只有真的答完(finish=stop)才壓:被截斷(length)/ 出錯 / 中斷的那一輪
        # 沒有可信的切點。
        if self.on_idle is not None and result.finish == client_events.REASON_STOP:
            try:
                message = self.on_idle()
            except Exception as exc:  # noqa: BLE001 - 壓縮失敗不得帶走 REPL
                message = f"壓縮失敗:{type(exc).__name__}: {exc}"
            if message:
                self._note(message)

    def _session_changed(self) -> None:
        """換了 session 就重綁。

        `/new` 與 `/resume` 只換 engine 的 session_id 與 messages,壓縮器是同一個
        物件:不重綁的話上一段的摘要會進新對話的摘要請求,上一段的停用也會把
        新的一段停掉。這裡**不**呼叫 before_send:那會把「每個 session 只講一次」
        的停用警告在這裡消耗掉,真正送出時就沒有了。
        """
        if self.on_session_change is None:
            return
        try:
            self.on_session_change()
        except Exception:  # noqa: BLE001 - 重綁失敗不得帶走 REPL
            pass

    def _command(self, line: str) -> bool | None:
        name, _, argument = line[1:].partition(" ")
        name = name.strip().lower()
        argument = argument.strip()
        if name in ("exit", "quit"):
            return False
        if name == "help":
            print(HELP)
            return None
        if name == "thinking":
            self.show_reasoning = not self.show_reasoning
            self._note(f"thinking 顯示: {'開' if self.show_reasoning else '關'}")
            return None
        if name == "tools":
            for index, spec in enumerate(self.engine.tool_specs.values(), start=1):
                flag = "ro" if spec.read_only else "rw"
                print(f"  {index:2d}. [{flag}] {spec.name}")
            return None
        if name == "new":
            try:
                self.engine.new_session()
            except Exception as exc:  # noqa: BLE001 - 建不了新 session 就留在原地
                self._note(f"無法開新對話: {exc}(仍在 {self.engine.session_id})")
                return None
            self._session_changed()
            self._note(f"新對話: {self.engine.session_id}")
            return None
        if name == "sessions":
            sessions = self.engine.store.list_sessions()
            if not sessions:
                self._note("這個專案還沒有已保存的對話。")
                return None
            for info in sessions[:20]:
                print(f"  {info.session_id}  turns={info.turns}  {info.title}")
            return None
        if name == "resume":
            if not argument:
                self._note("用法: /resume <session id>")
                return None
            try:
                self.engine.resume(argument)
            except Exception as exc:  # noqa: BLE001
                self._note(f"無法接續: {exc}")
                return None
            self._session_changed()
            self._note(f"已接續 {argument}({len(self.engine.messages)} 則訊息)")
            return None
        if name == "compact":
            if self.compact is None:
                self._note("這個 session 沒有可用的壓縮(模式為 off)。")
                return None
            self._note(self.compact())
            return None
        if name == "status":
            lines = self.status() if self.status else ()
            for entry in lines:
                self._note(entry)
            self._note(f"session={self.engine.session_id}")
            path = self.engine.store.path(self.engine.session_id)
            self._note(f"session 檔={path if path else '(不落檔)'}")
            if self.engine.store_error:
                self._note(
                    f"⚠ session 落檔失敗({self.engine.store_error});"
                    "這段對話只在記憶體裡,重開之後會消失。"
                )
            return None
        self._note(f"未知指令 {line.split()[0]};/help 看清單。")
        return None


def _short(value: Any, limit: int = 60) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def state_directory(root: str | os.PathLike[str]) -> Path:
    return client_store.sessions_dir(root)
