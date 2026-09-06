#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_compaction — 結構化壓縮(整組住在這裡,舊世代前端的 JS plugin 已不存在)。

搬過來之後消失的東西:``idle → summarize`` 之間的兩種競態,以及舊世代前端的
版本閘。迴圈是我們自己的:壓縮發生在「助理答完、沒有進行中的請求」的那一刻,
中間沒有另一個行程能插進來,所以 ``race_parent_mismatch`` /
``race_unanswered_user`` 由構造消失,不再需要事後偵測。

**留下來的**是真正與模型有關的那幾條(docs/compaction-rules.md 仍是唯一來源):
  * 七條摘要規則,逐字取自文件的 ```text 區塊;
  * 狀態校正節錄(最近五個已完成回合,50/30/10/5/5 配額,零工具參數與輸出);
  * 門檻公式(``compaction_formula.derive_settings``,與設定寫入端同一份);
  * 事後核對:空 / 只有 reasoning / 出錯 / 七欄格式漂移;
  * 停用 ledger:跨行程保留,只記不可信的那幾種成因。

模式:``codetrail``(idle 自動)/ ``manual``(只有 ``/compact``)/ ``off``。
native 在這裡沒有對應物 —— 沒有別人可以交回去。
"""
from __future__ import annotations

import hashlib
import contextlib
import json
import os
import re
import stat
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import client_events
import client_paths
import compaction_formula
import config
import context_budget
import llama_client

MODE_CODETRAIL = "codetrail"
MODE_MANUAL = "manual"
MODE_OFF = "off"
MODES = (MODE_CODETRAIL, MODE_MANUAL, MODE_OFF)

#: 沒有 client.json 時的模式。沒有設定檔 = 沒有接管(fail-closed)。
DEFAULT_MODE = MODE_MANUAL

STATE_DIR_NAME = "codetrail"
STOPPED_FILE = "compaction-stopped.jsonl"
STOPPED_ROTATED_FILE = "compaction-stopped.jsonl.1"
STOPPED_SCHEMA = 1
STOPPED_MAX_BYTES = 262_144

#: 只有「這個 session 的壓縮切點已經不可信」才寫成永久紀錄。
DURABLE_STOP_DETAILS = (
    "summary_empty",
    "summary_reasoning_only",
    "summary_error",
    "summary_format",
    "summary_truncated",
)

#: 狀態校正節錄:最近五個回合,由新到舊的字元配額。
RECONCILIATION_MAX_TURNS = 5
RECONCILIATION_QUOTAS = (0.50, 0.30, 0.10, 0.05, 0.05)
RECONCILIATION_BUDGET_CHARS = 4_000
TRUNCATION_MARKER = "…[截斷]"

SUMMARY_PREFIX = "[先前對話摘要]"

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*$")
_HEADING_ORDINAL_RE = re.compile(r"^\d+\s*[.、)）]\s*")


class CompactionError(RuntimeError):
    """壓縮設定或規則文件不合契約。"""


# ============================================================
# 門檻
# ============================================================
def derive(n_ctx: int, max_output: int | None = None) -> compaction_formula.DerivedSettings:
    """用**同一條**公式推導門檻。

    客戶端沒有 ``limit.input``,所以走 ``usable = context - max_output`` 那一支;
    ``max_output`` 是 ``config.CLIENT_MAX_OUTPUT_TOKENS`` —— 與實送的
    ``max_tokens``、context gate 的保留額是同一個常數。
    """
    output = config.CLIENT_MAX_OUTPUT_TOKENS if max_output is None else max_output
    return compaction_formula.derive_settings(context_limit=n_ctx, output_limit=output)


if config.CLIENT_MAX_OUTPUT_TOKENS_CAP != compaction_formula.OUTPUT_TOKEN_MAX:
    # pragma: no cover - import guard
    # 兩邊分開改就是「送 65536、門檻按 32000 算」那個 bug 的原型。
    raise RuntimeError(
        "CLIENT_MAX_OUTPUT_TOKENS_CAP 必須等於 compaction_formula.OUTPUT_TOKEN_MAX"
    )


# ============================================================
# 規則與節錄
# ============================================================
def rules_block() -> str:
    return compaction_formula.canonical_block(compaction_formula.RULES_BLOCK_MARKER)


def reconciliation_header() -> str:
    return compaction_formula.canonical_block(compaction_formula.RECONCILIATION_BLOCK_MARKER)


def rule_headings() -> tuple[str, ...]:
    return compaction_formula.rule_headings()


def _summary_headings(text: str) -> list[str]:
    found: list[str] = []
    for line in str(text or "").split("\n"):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        found.append(_HEADING_ORDINAL_RE.sub("", match.group(1)))
    return found


def summary_follows_contract(text: str, headings: Sequence[str] | None = None) -> bool:
    """七欄契約:七個都在、相對順序一致。

    刻意比字面規則寬三處:多出來的標題不算違規、``#`` 的層級不算、標題後面多
    的裝飾(``## 任務 (Task)``)也不算。成本不對稱 —— 漏抓只是那一次的分離沒
    了,誤抓會把一個內容完全可用的 session 停掉。實際發生過的漂移是整份換成
    另一套欄位,七個一個都對不上,這些寬容都擋不掉它。
    """
    contract = tuple(headings) if headings is not None else rule_headings()
    if not contract:
        return True
    found = _summary_headings(text)
    cursor = 0
    for heading in contract:
        index = next(
            (
                position
                for position, name in enumerate(found)
                if position >= cursor and name.startswith(heading)
            ),
            None,
        )
        if index is None:
            return False
        cursor = index + 1
    return True


def is_completed_answer(message: Mapping[str, Any]) -> bool:
    """這一則 assistant 訊息是不是一個**完整**的答案。

    engine 會把截斷(length)、出錯、空白的那些標 ``tool_status=error``;
    有工具呼叫的是中間步驟;空字串也不是答案。
    """
    if message.get("role") != "assistant" or message.get("tool_calls"):
        return False
    if message.get("tool_status") == client_events.STATUS_ERROR:
        return False
    content = message.get("content")
    return isinstance(content, str) and bool(content.strip())


def last_user_answered(messages: Sequence[Mapping[str, Any]]) -> bool:
    """最後一則**真實**使用者訊息有沒有被完整回答。沒有使用者訊息算已回答。"""
    last_user = None
    for index, message in enumerate(messages):
        if message.get("role") == "user" and not message.get("synthetic"):
            last_user = index
    if last_user is None:
        return True
    return any(is_completed_answer(message) for message in messages[last_user + 1:])


@dataclass(frozen=True)
class Turn:
    """一個已完成的真實回合(使用者問句 + 助理回答)。"""

    question: str
    answer: str


def completed_turns(messages: Sequence[Mapping[str, Any]]) -> list[Turn]:
    """最近的**已完成**真實回合,新→舊。

    排除:還沒被回答的那則(pending)、synthetic 的摘要注入、出錯或被中斷的
    回合。這些一旦混進來,摘要器就會把還沒做的事當成已完成。
    **不放工具參數與工具輸出** —— 節錄只用來校正狀態,不是第二份證據。
    """
    turns: list[Turn] = []
    pending: str | None = None
    for message in messages:
        role = message.get("role")
        if role == "user":
            if message.get("synthetic"):
                pending = None
                continue
            pending = str(message.get("content") or "")
            continue
        if role != "assistant" or pending is None:
            continue
        if message.get("tool_calls"):
            # 工具迴圈的中間步驟不是答案。
            continue
        if message.get("tool_status") == client_events.STATUS_ERROR:
            pending = None
            continue
        answer = message.get("content")
        if not isinstance(answer, str) or not answer.strip():
            pending = None
            continue
        turns.append(Turn(pending, answer))
        pending = None
    turns.reverse()
    return turns[:RECONCILIATION_MAX_TURNS]


def _clip(text: str, budget: int) -> str:
    text = " ".join(str(text or "").split())
    if budget <= len(TRUNCATION_MARKER):
        return TRUNCATION_MARKER
    if len(text) <= budget:
        return text
    return text[: budget - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def reconciliation_block(
    messages: Sequence[Mapping[str, Any]], *, budget: int = RECONCILIATION_BUDGET_CHARS
) -> str:
    """組出狀態校正節錄。沒有可用回合時回空字串。"""
    turns = completed_turns(messages)
    if not turns:
        return ""
    lines = [reconciliation_header(), ""]
    for index, turn in enumerate(turns):
        quota = int(budget * RECONCILIATION_QUOTAS[min(index, len(RECONCILIATION_QUOTAS) - 1)])
        question = _clip(turn.question, max(40, quota // 3))
        answer = _clip(turn.answer, max(40, quota - quota // 3))
        lines.append(f"- 問:{question}")
        lines.append(f"  答:{answer}")
    return "\n".join(lines)


# ============================================================
# 停用 ledger
# ============================================================
def state_dir(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    base = str(environ.get("XDG_STATE_HOME", "")).strip()
    root = Path(base) if base else Path(environ.get("HOME", str(Path.home()))) / ".local" / "state"
    return root / STATE_DIR_NAME


class _LedgerError(RuntimeError):
    """ledger 的路徑或權限不合契約。呼叫端一律 fail-open。"""


def _ledger_error(message: str) -> _LedgerError:
    return _LedgerError(message)


def session_hash(session_id: str) -> str:
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()


def record_stopped(session_id: str, detail: str, env: Mapping[str, str] | None = None) -> bool:
    """記下「這個 session 已停用自動壓縮」。只記不可信的那幾種成因。

    零內容:session 只留雜湊,detail 是固定 slug。寫不了就算了 —— 一個寫不了
    的紀錄不該把整個 session 的壓縮鎖死(fail-open)。
    """
    if detail not in DURABLE_STOP_DETAILS:
        return False
    try:
        directory = state_dir(env)
        anchor = directory.parent
        dir_fd = client_paths.open_private_dir(directory, _ledger_error, anchor=anchor)
        try:
            # rotate 也要錨在 dir fd 上:path-based 的 os.replace 會跟著
            # 被換成 symlink 的名字走,把別人的檔案改名。
            try:
                info = os.stat(STOPPED_FILE, dir_fd=dir_fd, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_size >= STOPPED_MAX_BYTES:
                    os.replace(
                        STOPPED_FILE, STOPPED_ROTATED_FILE,
                        src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
                    )
            except OSError:
                pass
        finally:
            os.close(dir_fd)
        line = json.dumps(
            {
                "schema": STOPPED_SCHEMA,
                "ts": time.time(),
                "session": session_hash(session_id),
                "detail": detail,
            },
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n"
        client_paths.append_private_line(
            directory, STOPPED_FILE, line.encode("utf-8"), _ledger_error, anchor=anchor
        )
        return True
    except (OSError, _LedgerError):
        # 寫不了的紀錄不該把整個 session 的壓縮鎖死(fail-open)。
        return False


def read_stopped(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """讀停用紀錄,回 ``{session 雜湊: 最後一次的 detail}``。讀不到當成沒有。"""
    found: dict[str, str] = {}
    directory = state_dir(env)
    for name in (STOPPED_ROTATED_FILE, STOPPED_FILE):
        try:
            # 讀取端用同一套防線:被換成 symlink 的 ledger 讀回來的是別人的
            # 內容,而它的作用是「停用這個 session 的自動壓縮」。
            payload = client_paths.read_private_file(
                directory, name, _ledger_error, max_bytes=STOPPED_MAX_BYTES * 2,
                anchor=directory.parent,
            )
        except (OSError, _LedgerError):
            continue
        if payload is None:
            continue
        try:
            raw = payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for line in raw.split("\n"):
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict) or parsed.get("schema") != STOPPED_SCHEMA:
                continue
            session = parsed.get("session")
            if not isinstance(session, str) or not session:
                continue
            detail = parsed.get("detail")
            found[session] = detail if detail in DURABLE_STOP_DETAILS else "unknown"
    return found


# ============================================================
# 壓縮
# ============================================================
@dataclass
class CompactionOutcome:
    status: str  # "compacted" | "skipped" | "stopped"
    detail: str = ""
    message: str = ""
    summary_chars: int = 0
    dropped_messages: int = 0


RESEND_HINT = (
    "請開一個新的對話,把畫面上還看得到的問題與必要狀態重送一次。"
    "不會自動重試,也沒有恢復舊 context 這回事。"
)

_STOP_MESSAGES = {
    "summary_empty": "壓縮產生了空摘要,這個 session 的自動壓縮已停用。",
    "summary_reasoning_only": "壓縮只產生了 thinking、沒有摘要本文,這個 session 的自動壓縮已停用。",
    "summary_error": "壓縮請求出錯,這個 session 的自動壓縮已停用。",
    "summary_format": (
        "摘要沒有照七欄格式輸出(已確定事實 vs 未確認的分離失效),"
        "這個 session 的自動壓縮已停用;摘要沒有落地,對話可以繼續。"
    ),
}


def stop_message(detail: str) -> str:
    base = _STOP_MESSAGES.get(detail, f"壓縮停止({detail})。")
    if detail == "summary_format":
        return base + "要繼續用結構化壓縮就開一個新對話;同一個模型一直不遵守就把模式切成 off。"
    return base + RESEND_HINT


def build_summary_messages(
    history: Sequence[Mapping[str, Any]],
    *,
    previous_summary: str = "",
    system_prompt: str = "",
) -> list[dict[str, Any]]:
    """組出摘要請求。七條規則**附加**在指示之後,前一份摘要一定要進去。"""
    instructions = [
        "你的任務是把下面這段對話壓縮成一份結構化摘要,讓另一個助理可以只憑摘要接手。",
        "",
        rules_block(),
    ]
    excerpt = reconciliation_block(history)
    if excerpt:
        instructions.extend(["", excerpt])
    if previous_summary:
        instructions.extend(
            [
                "",
                "[先前摘要](既有事實;與新內容衝突時以新內容為準,並註明哪一條被取代)",
                previous_summary,
            ]
        )
    transcript = serialise_history(history)
    return [
        {"role": "system", "content": (system_prompt + "\n\n" if system_prompt else "") + "\n".join(instructions)},
        {"role": "user", "content": transcript},
    ]


def serialise_history(history: Sequence[Mapping[str, Any]]) -> str:
    """把要被摘要的那段對話序列化成文字。

    工具結果保留(它們是「已確定事實」的來源),但工具**參數**不放 —— 參數
    對摘要沒有幫助,卻會把路徑與 patch 內容再抄一份進摘要請求。
    """
    lines: list[str] = []
    for message in history:
        role = message.get("role")
        content = message.get("content")
        if role == "user":
            lines.append(f"[使用者] {content}")
        elif role == "assistant":
            if isinstance(content, str) and content.strip():
                lines.append(f"[助理] {content}")
            for call in message.get("tool_calls") or ():
                function = call.get("function") if isinstance(call, Mapping) else None
                name = function.get("name") if isinstance(function, Mapping) else "?"
                lines.append(f"[助理呼叫工具] {name}")
        elif role == "tool":
            lines.append(f"[工具結果 {message.get('name', '?')}] {content}")
    return "\n".join(lines)


def split_for_compaction(
    messages: Sequence[Mapping[str, Any]],
    *,
    preserve_recent_tokens: int,
    tail_turns: int = compaction_formula.TAIL_TURNS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """切成 (要摘要的前段, 逐字保留的 tail)。

    tail 從最後一個真實使用者訊息往後算 ``tail_turns`` 輪,並受
    ``preserve_recent_tokens`` 限制。整段對話只有一輪時 tail 是空的 ——
    那一輪已經被回答過,所以沒有未答的問題會遺失。
    """
    starts = [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "user" and not message.get("synthetic")
    ]
    if len(starts) <= tail_turns:
        return list(messages), []
    cut = starts[-tail_turns]
    tail = [dict(message) for message in messages[cut:]]
    budget_chars = max(0, preserve_recent_tokens) * config.CHARS_PER_TOKEN
    used = 0
    for message in tail:
        content = message.get("content")
        used += len(content) if isinstance(content, str) else 0
    if used > budget_chars and len(starts) > tail_turns:
        # tail 裝不下保留額 —— 呼叫端要講出來(見 DerivedSettings 的
        # tail_holds_a_full_headroom_turn),但仍照 tail_turns 切,不從回合中段
        # 切開(從中間切會讓使用者訊息落進摘要)。
        pass
    return [dict(message) for message in messages[:cut]], tail


# ============================================================
# Compactor(掛在 engine 上)
# ============================================================
class EngineLike(Protocol):
    """Compactor 對 engine 的**全部**依賴。

    寫成 protocol 而不是直接吃 `client_engine.Engine`,是為了讓壓縮的核對與
    節錄規則可以在沒有模型、沒有 MCP、沒有 session 檔的情況下被測到 —— 那幾條
    規則才是這個模組的安全層,不該只能在整條路徑通了之後才驗得到。
    """

    session_id: str
    messages: list[dict[str, Any]]

    def payload_messages(self) -> tuple[list[dict[str, Any]], dict[str, Any]]: ...

    def prune_for_summary(
        self, messages: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]: ...

    def openai_tools(self) -> list[dict[str, Any]]: ...

    def replace_history(self, messages: Sequence[Mapping[str, Any]]) -> None: ...

    def complete(self, messages: Sequence[Mapping[str, Any]], *, source: str) -> "Completion": ...


@dataclass(frozen=True)
class Completion:
    """一次非工具的模型呼叫結果。

    ``reasoning`` 分開帶是為了認得出 `summary_reasoning_only`:模型只吐了
    thinking、沒有摘要本文時,那不是「空回應」,而是一種特定的失敗。
    """

    text: str
    reasoning: str = ""
    #: 串流的 finish_reason。只有 `stop` 是完整的:`length` 是被 max_tokens 切掉,
    #: 空字串是串流沒有正常結束——兩種都不能拿來取代原始歷史。
    finish: str = "stop"


class Compactor:
    """一個對話的壓縮狀態機。

    觸發點是 **idle**:助理已經完整答完、沒有進行中的請求。這是搬進 Python
    之後唯一剩下的觸發條件 —— 迴圈是我們自己的,所以「壓縮到一半使用者又送
    了一則訊息」這種競態由構造消失,不需要事後偵測。
    """

    def __init__(
        self,
        engine: EngineLike,
        mode: str,
        *,
        n_ctx: int,
        max_output_tokens: int | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if mode not in MODES:
            raise CompactionError(f"未知的壓縮模式: {mode!r}")
        self.engine = engine
        self.mode = mode
        self.env = env
        self.n_ctx = n_ctx
        # 推不出可用門檻不是例外狀況,是一種要**講出來**的模式:n_ctx 太小的
        # 機器上,靜靜不壓縮等於這個 session 從此不再壓縮而沒有人知道。
        self.derived: compaction_formula.DerivedSettings | None = None
        self.derive_error = ""
        try:
            self.derived = derive(n_ctx, max_output_tokens)
        except compaction_formula.CompactionModeError as exc:
            self.derive_error = str(exc)
        self.previous_summary = ""
        self.last_anchor: str | None = None
        self.stopped_detail: str | None = None
        self._bound_session = ""
        self._announced_stop: str | None = None
        self.rebind()

    def rebind(self) -> None:
        """重新綁到 engine **目前**的 session。

        `/new` 與 `/resume` 只換 engine 的 session_id 與 messages,Compactor
        是同一個物件。不重綁的話:上一段對話的 `previous_summary` 會被送進
        新對話的摘要請求(NDA 內容跨 session 外洩),`last_anchor` 會擋掉新
        對話第一次該做的壓縮,而上一段的停用狀態會把新的一段也停掉 ——
        反過來,新 session 自己在 ledger 裡的停用紀錄則被忽略。
        """
        session_id = str(getattr(self.engine, "session_id", "") or "")
        if session_id == self._bound_session:
            return
        self._bound_session = session_id
        self.previous_summary = ""
        self.last_anchor = None
        self.stopped_detail = read_stopped(self.env).get(session_hash(session_id))

    def pending_stop_notice(self) -> str:
        """使用者**送出的當下**要講的停用警告。每個 session 只講一次。

        等整輪跑完再由 idle hook 講,是「這一輪先撞 context gate 就完全看不到
        原因」—— 而使用者看到的正是那個 gate 錯誤,卻不知道自動壓縮早就停了。
        """
        self.rebind()
        if self.mode != MODE_CODETRAIL or not self.stopped_detail:
            return ""
        if self._announced_stop == self._bound_session:
            return ""
        self._announced_stop = self._bound_session
        return stop_message(self.stopped_detail)

    # ---- 判定 ----------------------------------------------------------
    @property
    def usable(self) -> bool:
        """這個 n_ctx 推得出可用門檻嗎?"""
        return self.derived is not None

    def estimated_tokens(self) -> int:
        payload, _ = self.engine.payload_messages()
        tokens, _chars = context_budget.estimate_tokens(
            messages=payload, tools=self.engine.openai_tools()
        )
        return tokens

    def anchor(self) -> str | None:
        """這一次要用哪一則助理訊息當切點,回它的**身分**。

        身分不能用位置:壓縮之後索引全變了,同一則助理訊息會被當成新的錨點,
        於是每次 idle 都再壓一次,而且每一次都「成功」。優先用 engine 給的
        穩定 id,沒有就用 (時間, 內容雜湊)。
        """
        for message in reversed(self.engine.messages):
            if message.get("role") != "assistant" or message.get("tool_calls"):
                continue
            if not is_completed_answer(message):
                # 被截斷(length)、出錯、空白的那一則不是答案:拿它當切點,
                # 摘要就把一個沒答完的問題摘成「已完成」。
                continue
            identity = message.get("id")
            if identity:
                return f"id:{identity}"
            content = message.get("content")
            digest = hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()[:16]
            return f"{message.get('time', '')}:{digest}"
        return None

    def should_compact(self) -> tuple[bool, str]:
        if self.mode != MODE_CODETRAIL:
            return False, "mode"
        if self.stopped_detail:
            return False, "stopped"
        if not self.usable:
            return False, "threshold_unusable"
        anchor = self.anchor()
        if anchor is None:
            return False, "no_answer"
        if not last_user_answered(self.engine.messages):
            return False, "unanswered"
        if anchor == self.last_anchor:
            # 壓縮之後那則助理訊息的 token 數不會變小,不擋就會每次 idle 都
            # 再壓一次,而且每一次都「成功」。
            return False, "same_anchor"
        if self.estimated_tokens() <= self.derived.idle_threshold:
            return False, "below_threshold"
        return True, "over_threshold"

    # ---- 執行 ----------------------------------------------------------
    def compact(self, *, manual: bool = False) -> CompactionOutcome:
        """壓縮一次。**整個** compact(含 preflight 的每一個 early-return)都是 engine 的一個
        turn:取消的判定走 engine 同一套線性化——每一個「這次壓縮的結果就是這樣」的決定
        (early-return、換歷史、永久停用)之前先 commit_point(),之前的取消讓這裡以
        TurnCancelled 結束(歷史 / ledger 一個 byte 都不動),之後的取消一律被拒絕。
        preflight 若在 turn 之外,協調器已開始回合而 engine 還閒置,取消會被當成 prestart
        「武裝」、卻沒有任何 turn 去消費它——回了 ok 卻什麼都沒取消。"""
        scope = getattr(self.engine, "turn_scope", None)
        commit_point = getattr(self.engine, "commit_point", None)
        try:
            with (scope() if callable(scope) else contextlib.nullcontext()):
                return self._compact_in_turn(manual=manual, commit_point=commit_point)
        except client_events.TurnCancelled:
            # 使用者在壓縮進行中按了中斷:不是摘要不可信,不得記成永久停用。
            return CompactionOutcome("skipped", "cancelled", "壓縮被中斷,對話維持原狀。")

    @staticmethod
    def _settle(commit_point: Any, outcome: CompactionOutcome) -> CompactionOutcome:
        """early-return 也是一個寫定的決定:先過 commit point(取消先到 → TurnCancelled)。"""
        if callable(commit_point):
            commit_point()
        return outcome

    def _compact_in_turn(self, *, manual: bool, commit_point: Any) -> CompactionOutcome:
        if self.mode == MODE_OFF:
            return self._settle(commit_point, CompactionOutcome("skipped", "mode_off", "壓縮模式是 off,不會壓縮。"))
        if self.stopped_detail:
            return self._settle(commit_point, CompactionOutcome(
                "skipped", self.stopped_detail, stop_message(self.stopped_detail)
            ))
        if not self.usable:
            return self._settle(commit_point, CompactionOutcome(
                "skipped",
                "threshold_unusable",
                f"這個 n_ctx({self.n_ctx})推不出可用的壓縮門檻;"
                "請把 n_ctx 調大,或把壓縮模式切成 off。"
                + (f"\n  {self.derive_error}" if self.derive_error else ""),
            ))
        if not manual:
            ready, reason = self.should_compact()
            if not ready:
                return self._settle(commit_point, CompactionOutcome("skipped", reason))

        anchor = self.anchor()
        # manual 只跳過門檻與 same-anchor 兩條;「有沒有可信的切點」manual 也要守:
        # crash 後 resume 到只剩一則 pending user 的歷史再按 /compact,會把還沒
        # 回答的問題摘掉。
        if anchor is None:
            return self._settle(commit_point, CompactionOutcome(
                "skipped", "no_answer", "還沒有已完成的回合,沒有可信的壓縮切點。"
            ))
        if not last_user_answered(self.engine.messages):
            return self._settle(commit_point, CompactionOutcome(
                "skipped", "unanswered", "最後一則問題還沒有回答,不壓縮(壓了會把它摘掉)。"
            ))
        head, tail = split_for_compaction(
            self.engine.messages, preserve_recent_tokens=self.derived.preserve_recent_tokens
        )
        if not head:
            return self._settle(commit_point, CompactionOutcome("skipped", "nothing_to_summarise"))

        # 摘要請求送的是與一般 payload **同一份** pruned 內容;tail 則維持
        # 原文逐字保留(它是壓縮後要留在對話裡的那一段)。
        prune = getattr(self.engine, "prune_for_summary", None)
        summary_head = list(prune(head)) if callable(prune) else head

        try:
            completion = self._summarise(summary_head)
        except client_events.TurnCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - 任何失敗都走同一條停用路徑
            # 「永久停用」也是一個要寫定的決定:已接受的取消先於它就以中斷結束,
            # ledger 不寫(取消回了 True,持久狀態就不能再改)。
            if callable(commit_point):
                commit_point()
            return self._stop("summary_error", extra=f"({type(exc).__name__})")

        summary = completion.text
        detail = verify_summary(
            summary, reasoning=completion.reasoning, finish=getattr(completion, "finish", "stop")
        )
        if callable(commit_point):
            commit_point()          # 換歷史或停用,兩種都是寫定;之前的取消要算數
        if detail is not None:
            return self._stop(detail)
        return self._replace(head, tail, summary, anchor)

    def _replace(self, head, tail, summary: str, anchor: Any) -> CompactionOutcome:
        dropped = len(head)
        try:
            self.engine.replace_history(
                [
                    {
                        "role": "user",
                        "content": f"{SUMMARY_PREFIX}\n{summary}",
                        "synthetic": True,
                    }
                ]
                + tail
            )
        except Exception as exc:  # noqa: BLE001
            # 落檔失敗:記憶體歷史沒有被換掉(replace_history 先寫再換),
            # 錨點也不記 —— 下一次 idle 要能再試一次。
            return CompactionOutcome(
                "failed",
                "persist_error",
                f"壓縮沒有生效:新歷史寫不進 session 檔({type(exc).__name__}: {exc})。"
                "對話維持原狀,下一次 idle 會再試一次。",
            )
        # 只有真的換成功了才記:記早了會讓同一個錨點被當成壓過,而且下一次
        # 摘要請求會帶著一份其實沒有生效的 previous_summary。
        self.previous_summary = summary
        self.last_anchor = anchor
        return CompactionOutcome(
            "compacted",
            "",
            f"已壓縮:{dropped} 則訊息換成一份 {len(summary)} 字元的摘要,最近一輪逐字保留。",
            summary_chars=len(summary),
            dropped_messages=dropped,
        )

    def _summarise(self, head: Sequence[Mapping[str, Any]]) -> Completion:
        messages = build_summary_messages(
            head,
            previous_summary=self.previous_summary,
            system_prompt="",
        )
        # 摘要請求走**同一個** context gate 與同一把模型鎖(engine.complete
        # 負責):摘要器那一端一樣會被 llama-server 從前面靜默截掉。
        return self.engine.complete(messages, source="compaction")

    def _stop(self, detail: str, *, extra: str = "") -> CompactionOutcome:
        self.stopped_detail = detail
        record_stopped(self.engine.session_id, detail, self.env)
        try:
            import mcp_lease

            mcp_lease.record_incident(
                "compaction_stopped",
                session=self.engine.session_id,
                detail=detail,
                source="server",
            )
        except Exception:  # noqa: BLE001 - incident 寫不了不得擋住對話
            pass
        return CompactionOutcome("stopped", detail, stop_message(detail) + extra)


def verify_summary(text: str, *, reasoning: str = "", finish: str = "stop") -> str | None:
    """壓縮後的事後核對。回 None 代表通過,否則回失敗成因 slug。

    搬進 Python 之後只剩下與**模型產出**有關的三類:空 / 只有 reasoning /
    七欄格式漂移。兩種競態(`race_parent_mismatch` / `race_unanswered_user`)
    由構造消失 —— 摘要請求是我們自己在 idle 送出的,中間沒有另一個行程能插
    進來把對話切掉。
    """
    if not isinstance(text, str) or not text.strip():
        return "summary_reasoning_only" if str(reasoning or "").strip() else "summary_empty"
    if finish != "stop":
        # 七個標題都在,末欄卻被切掉:格式核對過得了,內容不完整。
        return "summary_truncated"
    if not summary_follows_contract(text):
        return "summary_format"
    return None


# 截斷的摘要:與 summary_format 同一類「這次的切點不可信」的成因。
_STOP_MESSAGES["summary_truncated"] = (
    "壓縮已停止:摘要沒有寫完(被輸出上限切掉或串流中途斷掉),不能拿來取代原始歷史。"
)
