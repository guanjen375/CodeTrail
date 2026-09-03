#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_engine — CodeTrail 聊天客戶端的引擎層。

負責 session 狀態、訊息組裝、工具迴圈、權限、事件流。**不負責畫面**:
終端 REPL、headless `run --format json` 與 web 都是這一層之上的薄前端。

三件事決定了這一層的形狀:

1. **送出去的那一份才算數。** 模型呼叫前的 payload 會先做 reasoning 剝除與
   舊工具輸出剪枝,context gate 對**剪過之後**的那一份計數,保留額用的是這
   一次 request 實送的 ``max_tokens``。session 檔與畫面保留原文。
2. **一把模型鎖。** llama-server 是單 slot;多對話、壓縮摘要與使用者訊息同時
   打模型只會排隊。鎖在這裡明講,好過在 server 端變成看不懂的等待。
3. **工具結果只有 text block 進模型。** evidence 工具的 ``structuredContent``
   是另一份完整資料,重複餵給模型等於把同一份證據算兩次 context;它只給
   UI 與 eval。
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

import client_events
import client_mcp
import client_notify
import client_policy
import client_prompt
import config
import context_budget
import llama_client
import mcp_lease
from client_policy import Decision, PermissionPolicy

#: 一輪對話裡最多讓模型連續呼叫幾次工具。超過就停下來並講明。
DEFAULT_MAX_TOOL_STEPS = 24

#: 舊工具輸出剪枝(上游 `compaction.prune` 的等價實作,門檻逐字沿用)。
PRUNE_PROTECT_TOKENS = 40_000
PRUNE_MINIMUM_TOKENS = 20_000
PRUNE_SKIP_USER_TURNS = 2
PRUNE_PLACEHOLDER = "[Old tool result content cleared]"

#: 關掉「舊回合 reasoning 不進模型」的逃生門(沿用既有 env 名)。
KEEP_REASONING_ENV = "CODETRAIL_KEEP_REASONING"

#: 每個 MCP instance(等於每個 AICODE_ROOT / 每個 llama-server)一把模型鎖。
#: llama-server 是單 slot;同一行程裡的每個對話各自 new 一把鎖的話,兩條對話
#: 會同時進到那個 slot,排隊變成 server 端看不懂的等待。用 WeakKeyDictionary
#: 是為了 client 被回收時鎖跟著消失。
_MODEL_LOCKS: "weakref.WeakKeyDictionary[Any, threading.Lock]" = weakref.WeakKeyDictionary()
_MODEL_LOCKS_GUARD = threading.Lock()


def model_lock_for(mcp: Any) -> threading.Lock:
    """取得這個 MCP instance 對應的那一把模型鎖(沒有就建一把)。"""
    with _MODEL_LOCKS_GUARD:
        lock = _MODEL_LOCKS.get(mcp)
        if lock is None:
            lock = threading.Lock()
            _MODEL_LOCKS[mcp] = lock
        return lock

_TRUTHY = ("1", "true", "yes", "on")


class EngineError(RuntimeError):
    """引擎層的錯誤(設定不完整、模型端點拒絕等)。"""


def keep_reasoning_enabled(env: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if env is None else env
    return str(environ.get(KEEP_REASONING_ENV, "")).strip().lower() in _TRUTHY


# ============================================================
# 訊息轉換(送出去的那一份)
# ============================================================
def latest_real_user_index(messages: Sequence[Mapping[str, Any]]) -> int:
    """最新一則**真實**使用者訊息的位置;找不到回 -1。

    「真實」= 使用者自己送的,不含壓縮摘要注入與系統補的訊息。
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "user" and not message.get("synthetic"):
            return index
    return -1


def strip_historical_reasoning(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """拿掉「最新一則真實使用者訊息之前」的 assistant reasoning。

    它之後的一律保留 —— 同一輪的工具迴圈仍然看得到自己上一步在想什麼
    (DeepSeek 系模板依賴同回合的 reasoning 往返)。

    **認不出那則使用者訊息就整段不動。** 這一層只動 reasoning 欄位:不新增、
    不重排、不碰其他欄位。
    """
    out = [dict(message) for message in messages]
    cutoff = latest_real_user_index(out)
    if cutoff < 0:
        return out
    for index in range(cutoff):
        message = out[index]
        if message.get("role") != "assistant":
            continue
        for key in context_budget.REASONING_FIELDS:
            message.pop(key, None)
    return out


def _tool_message_tokens(message: Mapping[str, Any]) -> int:
    content = message.get("content")
    return context_budget.chars_to_tokens(len(content) if isinstance(content, str) else 0)


def prune_old_tool_outputs(messages: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """把很舊的工具輸出換成一行,回傳 (新訊息, 被清掉幾則)。

    從最新往回走,跳過最近 ``PRUNE_SKIP_USER_TURNS`` 個 user turn;之後累計工具
    輸出的估算 tokens,超過 ``PRUNE_PROTECT_TOKENS`` 之後的那些是候選;候選總量
    超過 ``PRUNE_MINIMUM_TOKENS`` 時才真的執行。

    只改**送模型的那一份**;session 檔與畫面保留原文。
    """
    out = [dict(message) for message in messages]
    user_turns = 0
    accumulated = 0
    candidates: list[int] = []
    candidate_tokens = 0
    for index in range(len(out) - 1, -1, -1):
        message = out[index]
        if message.get("role") == "user":
            user_turns += 1
            continue
        if user_turns < PRUNE_SKIP_USER_TURNS:
            continue
        if message.get("role") != "tool":
            continue
        if message.get("content") == PRUNE_PLACEHOLDER:
            continue
        tokens = _tool_message_tokens(message)
        # 先累加**這一筆**再判定,與上游同序。反過來(先看舊的累計)會讓「單獨
        # 一筆就跨過門檻」的巨大工具結果整個受保護 —— 而那正是最該被清掉的
        # 那一筆。
        accumulated += tokens
        if accumulated > PRUNE_PROTECT_TOKENS:
            candidates.append(index)
            candidate_tokens += tokens
    if candidate_tokens <= PRUNE_MINIMUM_TOKENS:
        return out, 0
    for index in candidates:
        out[index]["content"] = PRUNE_PLACEHOLDER
    return out, len(candidates)


_INTERNAL_KEYS = frozenset({"time", "tool_status", "synthetic", "structured", "call_index"})


def pending_tool_call_ids(messages: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """assistant 宣告了、卻沒有對應 tool 結果的呼叫 (id, 工具名)。

    中斷(Ctrl-C / timeout)會留下這種懸空呼叫。帶著它送出下一次請求時,
    chat template 會看到一個沒有結果的 tool_call —— 輕則模型困惑,重則
    llama-server 直接以模板錯誤拒收整個請求。
    """
    answered = {
        message.get("tool_call_id")
        for message in messages
        if message.get("role") == "tool"
    }
    pending: list[tuple[str, str]] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or ():
            if not isinstance(call, Mapping):
                continue
            call_id = call.get("id")
            if not isinstance(call_id, str) or call_id in answered:
                continue
            function = call.get("function")
            name = function.get("name") if isinstance(function, Mapping) else ""
            pending.append((call_id, name if isinstance(name, str) else ""))
    return pending


class HistoryPersistError(RuntimeError):
    """壓縮後的新歷史寫不進 session 檔。壓縮必須整個失敗,不能只成功一半。"""


#: 這一輪被使用者中斷。類別住在 client_events(壓縮端要認得出它);這裡只是別名。
#: 終端 REPL 的 Ctrl-C 走的是 KeyboardInterrupt;web / attach 沒有 signal 可用,所以
#: 另有這條協作式路徑:串流每收到一個 chunk 就看一次旗標,進行中的 MCP 呼叫則直接走
#: client_mcp 的取消契約(notifications/cancelled → 寬限期 → SIGTERM)。
TurnCancelled = client_events.TurnCancelled


LENGTH_CUT_MESSAGE = (
    "模型的輸出被 max_tokens 切掉(finish_reason=length):這一則不完整,不會被當成答案;"
    "被切到一半的工具呼叫也不會執行。請縮小問題或提高 AICODE_CLIENT_MAX_OUTPUT_TOKENS。"
)

TRUNCATED_STREAM_MESSAGE = (
    "模型的回覆串流沒有正常結束(沒有 finish_reason 就斷了):這一則不完整,不會被當成答案。"
    "請重送一次;持續發生時檢查 llama-server 的連線與 log。"
)

EMPTY_ANSWER_MESSAGE = (
    "模型回了空白答案(finish=stop,但沒有文字也沒有工具呼叫)。請重送一次;"
    "持續發生時多半是 chat template 或取樣設定的問題。"
)

CANCELLED_TOOL_RESULT = (
    "status: error\n這次呼叫被中斷,沒有結果。\n"
    "next: 需要的話重新呼叫一次;不要假設它已經執行過。"
)


def heal_in_place(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把每個懸空 tool_call 的「已中斷」結果插在**宣告它的那則訊息之後**。

    順序是契約的一部分:OpenAI 形狀要求 tool 結果緊接在宣告它的 assistant
    訊息之後。補在整段尾端的話,resume 之後再送一則新問題就會排成
    `assistant(tool_calls) → user → tool`,chat template 可能直接拒收,
    也可能讓模型把結果配到錯的呼叫上。
    """
    answered = {
        message.get("tool_call_id")
        for message in messages
        if message.get("role") == "tool"
    }
    out: list[dict[str, Any]] = []
    for message in messages:
        out.append(dict(message))
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or ():
            if not isinstance(call, Mapping):
                continue
            call_id = call.get("id")
            if not isinstance(call_id, str) or call_id in answered:
                continue
            function = call.get("function")
            name = function.get("name") if isinstance(function, Mapping) else ""
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name if isinstance(name, str) else "",
                    "content": CANCELLED_TOOL_RESULT,
                }
            )
    return out


def heal_messages(messages: Sequence[Mapping[str, Any]]) -> int:
    """補完之後會有幾則訊息(只用來報告補了幾筆)。"""
    return len(heal_in_place(messages))


class CancelDecision(NamedTuple):
    """``request_cancel()`` 的結果:有沒有接受,以及要在鎖外取消的 MCP 呼叫(可能 None)。"""

    accepted: bool
    call: Any


class _ModelSlot:
    """共用 model lock 的租約(``with`` 用)。

    正常路徑:``__exit__`` 放鎖。被放棄的請求(取消時 headers 還沒到)呼叫
    ``hand_off()``:``__exit__`` 不放,改由背景在那個 request 真的結束、串流關掉之後
    ``release_late()``——這樣下一輪在 model lock 上排隊,不會跟舊請求重疊
    (llama-server 單 slot,舊請求可能還在它的 queue 裡)。``threading.Lock`` 不綁
    執行緒,背景執行緒放鎖是合法的。
    """

    def __init__(self, lock: threading.Lock, cancel: threading.Event | None = None) -> None:
        self._lock = lock
        self._cancel = cancel
        self._guard = threading.Lock()
        self._handed_off = False
        self._released = False

    def __enter__(self) -> "_ModelSlot":
        # 等鎖也要能取消:鎖可能被一個被放棄、還在等 headers 的舊請求租著(最長 600 秒)。
        # 排在後面的這一輪按取消時沒有任何 stream / MCP call 可關,只能靠這裡每 50 ms
        # 看一次旗標;看到就以 TurnCancelled 結束,不取鎖。
        if self._cancel is None:
            self._lock.acquire()
            return self
        while not self._lock.acquire(timeout=0.05):
            if self._cancel.is_set():
                raise TurnCancelled("這一輪已被使用者中斷")
        return self

    def __exit__(self, *_exc: Any) -> bool:
        with self._guard:
            if self._handed_off or self._released:
                return False
            self._released = True
        self._lock.release()
        return False

    def hand_off(self) -> None:
        with self._guard:
            self._handed_off = True

    def release_late(self) -> None:
        with self._guard:
            if self._released:
                return
            self._released = True
        self._lock.release()


def _close_quietly(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - 收不掉的串流不影響取消
            pass


def _guarded(stream: Any, cancel: threading.Event):
    """迭代串流;被 ``cancel()`` 從別的執行緒關掉時,把 I/O 例外翻成中斷。"""
    iterator = iter(stream)
    while True:
        if cancel.is_set():
            # 取消若落在兩次 next() 之間(含第一次之前),不再進去讀下一個 chunk。
            raise TurnCancelled("這一輪已被使用者中斷")
        try:
            chunk = next(iterator)
        except StopIteration:
            return
        except Exception as exc:  # noqa: BLE001
            if cancel.is_set():
                raise TurnCancelled("這一輪已被使用者中斷") from exc
            raise
        yield chunk


def to_wire(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """去掉只給 UI / session 檔看的欄位,留下真正送出去的形狀。"""
    wire: list[dict[str, Any]] = []
    for message in messages:
        item = {key: value for key, value in message.items() if key not in _INTERNAL_KEYS}
        if item.get("content") is None and not item.get("tool_calls"):
            item["content"] = ""
        wire.append(item)
    return wire


# ============================================================
# 引擎
# ============================================================
@dataclass
class EngineOptions:
    root: Path
    model: str
    base_url: str
    n_ctx: int
    max_output_tokens: int = config.CLIENT_MAX_OUTPUT_TOKENS
    temperature: float = 0.2
    max_tool_steps: int = DEFAULT_MAX_TOOL_STEPS
    keep_reasoning: bool = False
    prune: bool = True
    policy: PermissionPolicy = field(default_factory=client_policy.InteractivePolicy)
    request_timeout: int = 600


@dataclass
class ApprovalRequest:
    session_id: str
    tool: str
    arguments: dict[str, Any]

    def render(self) -> str:
        """核准框的完整內容。參數一律完整顯示(含整份 patch),不截斷。"""
        lines = [f"工具: {self.tool}"]
        for key in sorted(self.arguments):
            value = self.arguments[key]
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            lines.append(f"  {key} = {text}")
        return "\n".join(lines)


@dataclass
class TurnResult:
    text: str
    finish: str
    steps: int
    tool_calls: int
    denied: int = 0
    notices: tuple[str, ...] = ()


class Engine:
    """一個對話。多個 Engine 可以共用同一個 ``McpClient``。"""

    def __init__(
        self,
        options: EngineOptions,
        *,
        mcp: client_mcp.McpClient,
        store: Any,
        session_id: str | None = None,
        system_prompt: client_prompt.SystemPrompt | None = None,
        model_lock: threading.Lock | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.options = options
        self.mcp = mcp
        self.store = store
        self.env = dict(os.environ if env is None else env)
        self.system_prompt = system_prompt or client_prompt.build_system_prompt(
            options.root, env=self.env
        )
        # llama-server 是單 slot:同一個 MCP instance 上的所有對話與壓縮摘要
        # 共用一把鎖,排隊發生在這裡而不是在 server 的請求佇列裡。
        # 預設**依 mcp 取**,不是各自 new 一把 —— 後者等於沒有鎖。
        self.model_lock = model_lock or model_lock_for(mcp)
        self.messages: list[dict[str, Any]] = []
        self.session_id = session_id or store.create()
        #: 第一次 session 落檔失敗的原因。非 None 代表這段對話只在記憶體裡。
        self.store_error: str | None = None
        self._tool_specs: dict[str, client_mcp.ToolSpec] = {}
        self._openai_tools: list[dict[str, Any]] = []
        self._loaded_tools = False
        # 協作式取消:旗標 + 目前進行中的串流 / MCP 呼叫(給 cancel() 從別的
        # 執行緒收掉)。旗標在這一輪收尾(_end_turn,計數歸零)時清,不在 send() 開頭清。
        self._cancel = threading.Event()
        self._active_lock = threading.Lock()
        self._active_stream: Any = None
        self._in_turn = 0                      # 進行中的 send() / complete() / turn_scope 數(可巢狀)
        self._turn_completed = False           # 這一輪的答案 / 摘要已經**決定寫定**(之後的取消一律拒絕)
        self._turn_seen_since_clear = False    # 自上次 clear_cancel() 起 engine 開始過 turn(不管是寫定、失敗或中斷):
                                               # 閒置期的取消一律拒絕——「閒置且沒開始過」才是 worker 還沒進 send()
        self._armed = False                    # web 在 worker 進 send() 之前就取消:下一個 turn 一開始就中斷
        # 取消的線性化鎖:_in_turn / _turn_completed / _turn_seen_since_clear / _armed 與
        # 「決定寫定」(_decide_commit)都在這把鎖裡;真正的 _record()(store append + fsync)
        # 在鎖外。request_cancel() 在鎖內原子決定接受 / 拒絕 / 武裝(見它的 docstring),
        # cancel() 與 web 的 WebApp.cancel() 都走它;clear_cancel() 是 web 的回合邊界。
        # 於是取消與寫定只有兩種順序:取消先到 → 這一則不寫、以 TurnCancelled 結束;寫定先
        # 決定 → 取消拒絕。不會有「回 True 卻保留答案」或「收尾清完旗標才補設」。
        self._turn_state = threading.Lock()
        self._active_call: Any = None

    # ---- tools ---------------------------------------------------------
    def load_tools(self) -> None:
        specs = self.mcp.tools()
        client_mcp.assert_public_catalog(specs)
        self._tool_specs = {spec.name: spec for spec in specs}
        self._openai_tools = [spec.as_openai_tool() for spec in specs]
        self._loaded_tools = True

    @property
    def tool_specs(self) -> dict[str, client_mcp.ToolSpec]:
        if not self._loaded_tools:
            self.load_tools()
        return self._tool_specs

    def openai_tools(self) -> list[dict[str, Any]]:
        if not self._loaded_tools:
            self.load_tools()
        return list(self._openai_tools)

    # ---- session -------------------------------------------------------
    def replace_history(self, messages: Sequence[Mapping[str, Any]]) -> None:
        """壓縮之後換掉 in-memory 歷史,並把新歷史整段追加進 session 檔。

        session 檔是 append-only,所以壓縮寫的是一筆 ``compaction`` 記錄,
        內容就是新的完整歷史(摘要 + 逐字保留的 tail)。resume 讀到最後一筆
        compaction 就從那裡接下去 —— 不然重開會把整段原始對話再吃回 context,
        壓縮等於白做。tail 的量由 ``preserve_recent_tokens`` 綁住,所以這份
        重複是有上限的。

        **先落檔再換記憶體**。順序反過來的話,append 失敗時畫面已經宣告
        壓縮成功、in-memory 歷史也已經被換掉,但重開會拿回完整的原始歷史 ——
        使用者看到的與檔案裡的從此不是同一段對話。這個 session 一開始就落不了
        檔(``store_error`` 已設)時例外:那段對話本來就只在記憶體裡,
        使用者也已經被警告過,壓縮不該因此被鎖死。
        """
        new_history = [dict(message) for message in messages]
        if self.store_error is None:
            try:
                self.store.append(
                    self.session_id,
                    {"type": "compaction", "time": time.time(), "history": new_history},
                )
            except Exception as exc:  # noqa: BLE001
                self.store_error = f"{type(exc).__name__}: {exc}"
                raise HistoryPersistError(f"{type(exc).__name__}: {exc}") from exc
        self.messages = new_history

    def prune_for_summary(
        self, messages: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """摘要請求要送的那一份:和一般 payload 走**同一套**轉換。

        摘要器直接吃原始歷史的話,已經被 prune 掉的舊工具輸出會整份回到模型
        面前 —— 摘要請求自己撞 context gate、壓縮從此停用,而且違反
        `docs/compaction-rules.md` 的「被清掉的工具結果進不了下一次摘要」。
        """
        working = heal_in_place(messages)
        if not (self.options.keep_reasoning or keep_reasoning_enabled(self.env)):
            working = strip_historical_reasoning(working)
        if self.options.prune:
            working, _pruned = prune_old_tool_outputs(working)
        return working

    def complete(self, messages: Sequence[Mapping[str, Any]], *, source: str):
        """一次非工具的模型呼叫(壓縮摘要用)。走同一個 gate 與同一把鎖。"""
        payload = [dict(message) for message in messages]
        usage = context_budget.check_and_log(
            source=source,
            requested_num_ctx=self.options.n_ctx,
            messages=payload,
            model=self.options.model,
            reserved_output_tokens=self.options.max_output_tokens,
            emit=False,
        )
        content: list[str] = []
        reasoning: list[str] = []
        finish = ""
        try:
            self._begin_turn()
            with self._model_slot() as slot:
                # 壓縮是同一輪的尾巴:取消若落在 run_tool_loop 結束之後、摘要開始
                # 之前,這裡就要接住,不然摘要照常完成而旗標留到下一輪。
                if self._cancel.is_set():
                    raise TurnCancelled("這一輪已被使用者中斷")
                stream = self._open_stream(
                    lambda: llama_client.chat_completions(
                        base_url=self.options.base_url,
                        messages=payload,
                        model=self.options.model,
                        temperature=self.options.temperature,
                        top_p=config.CHAT_TOP_P,
                        top_k=config.CHAT_TOP_K,
                        min_p=config.CHAT_MIN_P,
                        stream=True,
                        extra={"max_tokens": self.options.max_output_tokens},
                        timeout=self.options.request_timeout,
                    ),
                    slot,
                )
                try:
                    try:
                        for chunk in _guarded(stream, self._cancel):
                            if self._cancel.is_set():
                                raise TurnCancelled("這一輪已被使用者中斷")
                            context_budget.parse_usage_from_stream_chunk(chunk, usage)
                            choices = chunk.get("choices") if isinstance(chunk, dict) else None
                            if not choices:
                                continue
                            choice = choices[0] or {}
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                content.append(delta["content"])
                            if delta.get("reasoning_content"):
                                reasoning.append(delta["reasoning_content"])
                            if choice.get("finish_reason"):
                                finish = str(choice["finish_reason"])
                    except llama_client.StreamProtocolError:
                        finish = ""          # 中間掉了一塊 = 沒寫完
                    with self._turn_state:
                        if self._cancel.is_set():
                            raise TurnCancelled("這一輪已被使用者中斷")
                        if self._in_turn == 1:
                            # 獨立呼叫(沒有外層 turn_scope)才在這裡決定寫定;在壓縮的
                            # turn_scope 裡,寫定的決定點是換歷史之前的 commit_point()——
                            # 摘要收完到換歷史之間的取消仍要算數(歷史還沒動)。
                            self._turn_completed = True
                finally:
                    close = getattr(stream, "close", None)
                    if callable(close):
                        close()
                    with self._active_lock:
                        self._active_stream = None
        finally:
            # 摘要是這一輪的最後一步:結束時清旗標,取消才不會留到下一題。
            self._end_turn()
        context_budget.log_metrics(usage)
        import client_compaction

        # 沒有 finish_reason 的串流是被截斷的(transport 中途斷、缺 [DONE]),不是 stop。
        return client_compaction.Completion("".join(content), "".join(reasoning), finish or "")

    def new_session(self) -> str:
        """開一個新對話(TUI 的 /new)。

        **先 create 成功才換狀態**:反過來的話 create 失敗(磁碟滿、權限)時
        記憶體歷史已經被清空,而使用者還停在舊 session。
        """
        session_id = self.store.create()
        self.messages = []
        self.store_error = None
        self.session_id = session_id
        return session_id

    def clear_cancel(self) -> None:
        """web 在一輪(含尾端的壓縮)確定結束後呼叫:任何來不及消費的取消旗標都不得留到下一題。

        同時重設「這個回合裡 engine 開始過 turn」與「預先武裝」兩個閒置期狀態——它們是
        web 回合邊界的一部分(下一個回合的真 prestart 才能再武裝)。
        """
        with self._turn_state:
            self._cancel.clear()
            self._armed = False
            self._turn_seen_since_clear = False

    def resume(self, session_id: str) -> None:
        """從 session 檔重建歷史,尊重最後一次壓縮的切點。

        不看 compaction 記錄的話,重開一個壓縮過的對話會把整段原始歷史再吃回
        context —— 壓縮等於白做,而且第一輪就可能撞到 gate。
        """
        records = self.store.read(session_id)
        # 先把整段歷史重建好,**最後**才一次換 session_id / messages / store_error:
        # 中途失敗(compaction.history 含非 dict 的項目)不得留下「id 是新的、
        # messages 是舊對話」的混合狀態——下一題會把舊對話寫進新 session。
        history: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            kind = record.get("type")
            if kind == "compaction":
                restored = record.get("history")
                if not isinstance(restored, list) or not all(
                    isinstance(item, Mapping) for item in restored
                ):
                    raise ValueError(f"session {session_id} 的第 {index} 筆 compaction 記錄壞掉")
                history = [dict(item) for item in restored]
            elif kind == "message":
                history.append({k: v for k, v in record.items() if k != "type"})
        self.session_id = session_id
        self.messages = history
        # store_error 是**這個** session 的事:上一段對話寫不進去,不代表換過來
        # 的這一段也寫不進去——不重設的話,新 session 的壓縮記錄會被靜默跳過。
        self.store_error = None

    def _record(self, message: Mapping[str, Any]) -> None:
        payload = dict(message)
        payload.setdefault("time", time.time())
        self.messages.append(payload)
        try:
            self.store.append(self.session_id, {"type": "message", **payload})
        except Exception as exc:  # noqa: BLE001
            # 落檔失敗不得讓對話中斷 —— 但**一定要講**。吞掉的話這段對話只活
            # 在記憶體裡,使用者照常問下去,重開之後整段消失而且從來沒有警告。
            if self.store_error is None:
                self.store_error = f"{type(exc).__name__}: {exc}"

    # ---- payload -------------------------------------------------------
    def payload_messages(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """組出這一次真的要送出去的訊息,並回報做了哪些轉換。"""
        working = list(self.messages)
        summary: dict[str, Any] = {}
        # 懸空的 tool_call 一定要在送出前補齊,而且要補在**宣告它的那則
        # assistant 訊息之後**(見 heal_pending_tool_calls)。補在整段尾端會排出
        # `assistant(tool_calls) → user → tool` 這種不合法的相鄰順序。
        healed = heal_messages(working)
        if healed != len(working):
            summary["healed_tool_calls"] = healed - len(working)
        working = heal_in_place(working)
        if not (self.options.keep_reasoning or keep_reasoning_enabled(self.env)):
            before = sum(
                1
                for message in working
                if any(key in message for key in context_budget.REASONING_FIELDS)
            )
            working = strip_historical_reasoning(working)
            after = sum(
                1
                for message in working
                if any(key in message for key in context_budget.REASONING_FIELDS)
            )
            # 記**真的被拿掉幾則**,不是一個「有跑過轉換」的旗標:後者會讓
            # telemetry 的 did_trim 每一輪都是 True,那個欄位就再也看不出
            # 這一次到底有沒有動過送出去的內容。
            if before != after:
                summary["stripped_reasoning"] = before - after
        if self.options.prune:
            working, pruned = prune_old_tool_outputs(working)
            if pruned:
                summary["pruned_tool_results"] = pruned
        payload = [{"role": "system", "content": self.system_prompt.text}]
        payload.extend(to_wire(working))
        return payload, summary

    # ---- one turn ------------------------------------------------------
    def send(
        self,
        text: str,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_text: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        approve: Callable[[ApprovalRequest], bool] | None = None,
    ) -> TurnResult:
        emit = on_event or (lambda _event: None)
        # 旗標**不在這裡清**:web 的 cancel 可能在 worker 還沒進到 send() 之前就到,
        # 開始時清掉就是 lost-cancel。改在這一輪收尾(_end_turn,計數歸零)時清。
        # resume 進來的歷史可能停在一個沒有結果的 tool_call(上次崩潰 / 中斷)。
        # 先補完再記新問題,順序才會是 assistant(tool_calls) → tool → user。
        # 從這裡開始就算「進行中」:寫 user 訊息(store append 可能慢)期間的 cancel() 也要算數,
        # 不然一個已經改動 session 歷史的 send() 被當成閒置,取消無效、模型請求照發。
        self._begin_turn()
        try:
            self.heal_pending_tool_calls()
            self._record({"role": "user", "content": text})
            return self.run_tool_loop(
                on_event=emit, on_text=on_text, on_reasoning=on_reasoning, approve=approve
            )
        finally:
            self._end_turn()

    def _model_slot(self) -> "_ModelSlot":
        """取共用 model lock 的租約:正常情況 ``with`` 結束就放;被放棄的請求(取消時
        headers 還沒到)把租約交給背景,等那個 request 真的結束、串流關掉才放——下一輪
        在 model lock 上排隊,不會跟一個還在 llama-server queue 裡的舊請求重疊。"""
        return _ModelSlot(self.model_lock, self._cancel)

    def _open_stream(self, request: Callable[[], Any], slot: "_ModelSlot | None" = None) -> Any:
        """發出串流請求並登記成 active stream——**等 response headers 期間也能取消**。

        `requests` 在拿到 headers 之前沒有任何可以關的東西(socket 藏在 adapter 裡),
        llama-server 單 slot 被別的請求佔著時 headers 可以晚很久。所以請求在小執行緒裡
        發,這裡每 50 ms 看一次取消旗標:被取消就放棄這個請求、立刻 ``TurnCancelled``
        ——但 **model lock 跟著那個請求走**(``slot.hand_off``):response 到了由背景關掉
        (server 偵測到斷線會中止生成)之後才放鎖,所以下一輪永遠排在舊請求之後,單
        slot 的序列化不會被取消打破。拿到 handle 之後、第一個 ``next()`` 之前再看一次
        旗標:取消若落在「登記之前」,關掉的必須是這個剛拿到的 handle,不能讓 worker
        在第一個 chunk 上再卡一次。
        """
        box: dict[str, Any] = {}
        done = threading.Event()
        abandoned = False

        def _run() -> None:
            try:
                box["stream"] = request()
            except BaseException as exc:  # noqa: BLE001 - 原樣交回 worker
                box["error"] = exc
            finally:
                done.set()

        def _settle_abandoned() -> None:
            # 恰好執行一次:關掉(可能已經到了的)串流,然後才把 model lock 交還。
            _close_quietly(box.get("stream"))
            if slot is not None:
                slot.release_late()

        def _abandon() -> None:
            nonlocal abandoned
            if abandoned:
                return
            abandoned = True
            if slot is not None:
                slot.hand_off()
            if done.is_set():
                _settle_abandoned()
            else:
                threading.Thread(
                    target=lambda: (done.wait(), _settle_abandoned()),
                    name="codetrail-abandon",
                    daemon=True,
                ).start()

        threading.Thread(target=_run, name="codetrail-http", daemon=True).start()
        try:
            while not done.wait(0.05):
                if self._cancel.is_set():
                    raise TurnCancelled("這一輪已被使用者中斷")
        except BaseException:
            # 取消與 KeyboardInterrupt(終端 REPL)都走這裡,而且只放棄一次。
            if done.is_set():
                _close_quietly(box.get("stream"))   # 剛好到了:直接關,鎖照常由 with 放
            else:
                _abandon()
            raise
        if "error" in box:
            raise box["error"]
        stream = box["stream"]
        with self._active_lock:
            self._active_stream = stream
        if self._cancel.is_set():
            _close_quietly(stream)
            with self._active_lock:
                self._active_stream = None
            raise TurnCancelled("這一輪已被使用者中斷")
        return stream

    def _begin_turn(self) -> None:
        with self._turn_state:
            self._in_turn += 1
            if self._in_turn == 1:
                self._turn_completed = False
                self._turn_seen_since_clear = True
                if self._armed:
                    # web 在 worker 進來之前就取消了:這一輪一開始就是中斷。
                    self._armed = False
                    self._cancel.set()

    def _end_turn(self) -> None:
        with self._turn_state:
            self._in_turn -= 1
            if self._in_turn == 0:
                self._cancel.clear()
                self._turn_completed = False

    def _decide_commit(self) -> None:
        """線性化點:決定「這一輪的答案 / 摘要寫定」。

        跟 ``request_cancel()`` 在同一把 ``_turn_state`` 鎖裡:取消先到 → 這裡看到旗標,
        以 ``TurnCancelled`` 結束、**什麼都不寫**;這裡先到 → 之後的取消一律拒絕。
        鎖裡只做決定,不做 I/O:真正的 ``_record()``(store append + fsync)在鎖外——
        不然 web 的 app lock 會跟著等 fsync。
        """
        with self._turn_state:
            if self._cancel.is_set():
                raise TurnCancelled("這一輪已被使用者中斷")
            self._turn_completed = True

    def _commit_final(self, message: dict[str, Any]) -> None:
        """寫入這一輪的最後一則 assistant:先在鎖內決定,再在鎖外落檔。"""
        self._decide_commit()
        self._record(message)

    @contextlib.contextmanager
    def turn_scope(self):
        """把一段工作(例如整個壓縮:摘要請求 + 核對 + 換歷史)當成一個 turn。

        期間的取消走同一套判定;呼叫端在真正改動歷史之前呼叫 ``commit_point()``。
        """
        self._begin_turn()
        try:
            yield self
        finally:
            self._end_turn()

    def commit_point(self) -> None:
        """turn_scope 內「要開始改歷史了」的線性化點(見 ``_decide_commit``)。"""
        self._decide_commit()

    def request_cancel(self, *, arm_when_idle: bool = False) -> "CancelDecision":
        """取消的**快速**部分,而且**自己做決定**(在 ``_turn_state`` 內,原子):

        - 有進行中的 turn、答案 / 摘要還沒決定寫定 → 接受:設旗標、關掉進行中的串流,
          回 ``CancelDecision(True, call)``;``call`` 是進行中的 MCP 呼叫(可能 ``None``),
          交給 ``cancel_pending()`` 在**鎖外**走完整取消契約(會等寬限期,不能在共用鎖裡做)。
        - 有進行中的 turn、但已決定寫定 → 拒絕(``accepted=False``),什麼都不動。
        - 閒置:``arm_when_idle=False``(``cancel()``)→ 拒絕。``arm_when_idle=True``(web,它
          自己管理回合邊界):這個 web 回合裡 engine **已經開始過** turn(答案已寫定、或 send()
          失敗 / 中斷退出、或壓縮還沒開始)→ 拒絕——沒有東西可取消,web 會照原結果收尾;
          engine 自 ``clear_cancel()`` 起**還沒開始過**任何 turn(worker 還沒進 send())→ 預先
          武裝,下一個 turn 一開始就中斷,接受。

        鎖裡不做 I/O(關串流是 socket shutdown,不阻塞);呼叫端可以在自己的鎖裡呼叫。
        關的是底層 socket / response(``llama_client.OpenAIStream.close()``),不是 generator:
        對另一個執行緒正在跑的 generator 呼叫 ``close()`` 只會得到 ``ValueError``。
        """
        with self._turn_state:
            if self._in_turn == 0:
                if not arm_when_idle or self._turn_seen_since_clear:
                    return CancelDecision(False, None)
                self._armed = True
                return CancelDecision(True, None)
            if self._turn_completed:
                return CancelDecision(False, None)
            self._cancel.set()
            with self._active_lock:
                call = self._active_call
                stream = self._active_stream
        if stream is not None:
            _close_quietly(stream)
        return CancelDecision(True, call)

    @staticmethod
    def cancel_pending(call: Any) -> bool:
        """取消的**慢速**部分:對進行中的 MCP 呼叫走完整取消契約(可能等寬限期)。"""
        if call is None:
            return False
        try:
            call.cancel("cancelled by user")
        except Exception:  # noqa: BLE001 - 取消送不出去由 result() 那端回報
            pass
        return True

    def cancel(self) -> bool:
        """從**別的執行緒**中斷目前這一輪(快速 + 慢速兩段一起做)。

        接受的話進行中的 MCP 呼叫走完整取消契約、進行中的 HTTP 串流直接關掉(llama-server
        的 slot 才會放出來),這一輪會以 ``TurnCancelled`` 結束、答案不寫,回 ``True``;
        沒有進行中的一輪、或答案已決定寫定 → 什麼都不做,回 ``False``。
        """
        decision = self.request_cancel()
        if not decision.accepted:
            return False
        self.cancel_pending(decision.call)
        return True

    def heal_pending_tool_calls(self) -> int:
        """把懸空的 tool_call 補上「已中斷」結果並寫進 session 檔。"""
        pending = pending_tool_call_ids(self.messages)
        for call_id, name in pending:
            self._record(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": CANCELLED_TOOL_RESULT,
                    "tool_status": client_events.STATUS_ERROR,
                }
            )
        return len(pending)

    def run_tool_loop(
        self,
        *,
        on_event: Callable[[dict[str, Any]], None],
        on_text: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        approve: Callable[[ApprovalRequest], bool] | None = None,
    ) -> TurnResult:
        try:
            self._begin_turn()
            return self._run_tool_loop(
                on_event=on_event, on_text=on_text, on_reasoning=on_reasoning, approve=approve
            )
        except BaseException:
            # 中斷 / 例外都要把懸空呼叫收乾淨,否則下一輪的 payload 帶著一個
            # 沒有結果的 tool_call 送出去。
            self.heal_pending_tool_calls()
            raise
        finally:
            # 這一輪結束才清旗標(見 send());web 只在 turn 進行中才會呼叫
            # cancel(),所以不會有「沒在跑卻被取消」的旗標留到下一輪。
            self._end_turn()

    def _run_tool_loop(
        self,
        *,
        on_event: Callable[[dict[str, Any]], None],
        on_text: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        approve: Callable[[ApprovalRequest], bool] | None = None,
    ) -> TurnResult:
        specs = self.tool_specs
        steps = 0
        tool_calls = 0
        denied = 0
        notices: list[str] = []
        denied_counts: dict[str, int] = {}
        final_text = ""
        finish = client_events.REASON_STOP

        while steps < self.options.max_tool_steps:
            if self._cancel.is_set():
                raise TurnCancelled("這一輪已被使用者中斷")
            steps += 1
            step = self._one_model_step(on_text=on_text, on_reasoning=on_reasoning)
            final_text = step["content"] or final_text
            if step["content"]:
                on_event(client_events.text_event(self.session_id, step["content"]))

            calls = step["tool_calls"]
            if not calls:
                finish = step["finish"] or client_events.REASON_STOP
                if step.get("truncated"):
                    reason = step.get("truncation_reason") or ""
                    final_text = TRUNCATED_STREAM_MESSAGE + (f"({reason})" if reason else "")
                    notices.append(final_text)
                    on_event(client_events.text_event(self.session_id, final_text))
                elif step.get("cut"):
                    notices.append(LENGTH_CUT_MESSAGE)
                    on_event(client_events.text_event(self.session_id, LENGTH_CUT_MESSAGE))
                elif finish == client_events.REASON_STOP and not (step["content"] or "").strip():
                    # 空白答案:finish=stop 卻沒有文字也沒有工具呼叫。靜默送
                    # terminal 會讓使用者(與 eval)以為這一輪完成了。
                    finish = client_events.REASON_ERROR
                    final_text = EMPTY_ANSWER_MESSAGE
                    notices.append(EMPTY_ANSWER_MESSAGE)
                    on_event(client_events.text_event(self.session_id, final_text))
                on_event(
                    client_events.step_finish_event(
                        self.session_id, reason=finish, tokens=step["tokens"]
                    )
                )
                # 判準是**這一則** assistant 回應有沒有結構化 tool call,不是
                # 整輪曾經呼叫過。前面步驟真的呼叫過工具,不會讓最後這則
                # 「我現在來呼叫 read_file」變成不是假承諾。
                if client_notify.claims_tool_call(step["content"]):
                    notices.append(client_notify.PROMISE_WITHOUT_CALL_MESSAGE)
                    mcp_lease.record_incident(
                        "promise_without_call",
                        session=self.session_id,
                        detail="no_tool_part",
                        source="server",
                    )
                break

            on_event(
                client_events.step_finish_event(
                    self.session_id,
                    reason=client_events.REASON_TOOL_CALLS,
                    tokens=step["tokens"],
                )
            )
            for call in calls:
                tool_calls += 1
                outcome = self._run_one_tool(
                    call, specs=specs, approve=approve, denied_counts=denied_counts
                )
                if outcome["status"] == client_events.STATUS_DENIED:
                    denied += 1
                if outcome["notice"]:
                    notices.append(outcome["notice"])
                on_event(
                    client_events.tool_event(
                        self.session_id,
                        tool=call["name"],
                        call_id=call["id"],
                        status=outcome["status"],
                        arguments=call["arguments"],
                    )
                )
        else:
            finish = client_events.REASON_ERROR
            final_text = (
                f"[已停止] 這一輪連續問了模型 {self.options.max_tool_steps} 次、每次都只回工具呼叫,仍未收斂。"
                "請縮小問題範圍,或明確指定要用哪個工具。"
            )
            # 標成 error:這不是一個答案。沒有標記的話,狀態校正節錄會把
            # 「連續 N 次只回工具呼叫、已停止」當成已完成回合,摘要器於是把
            # 仍待處理的事項當成做完了。
            # 這也是這一輪的最後一則:走同一個線性化點(取消先到就不寫)。
            self._commit_final(
                {
                    "role": "assistant",
                    "content": final_text,
                    "tool_status": client_events.STATUS_ERROR,
                }
            )
            on_event(client_events.text_event(self.session_id, final_text))
            on_event(client_events.step_finish_event(self.session_id, reason=finish))

        if self.store_error is not None:
            notices.append(
                f"⚠ 這段對話沒有落檔({self.store_error});重開之後會消失。"
            )
        return TurnResult(
            text=final_text,
            finish=finish,
            steps=steps,
            tool_calls=tool_calls,
            denied=denied,
            notices=tuple(notices),
        )

    # ---- model ---------------------------------------------------------
    def _one_model_step(
        self,
        *,
        on_text: Callable[[str], None] | None,
        on_reasoning: Callable[[str], None] | None,
    ) -> dict[str, Any]:
        payload, transform = self.payload_messages()
        usage = context_budget.check_and_log(
            source="client",
            requested_num_ctx=self.options.n_ctx,
            messages=payload,
            tools=self._openai_tools,
            model=self.options.model,
            reserved_output_tokens=self.options.max_output_tokens,
            did_trim=bool(transform),
            trim_summary=transform,
            emit=False,
        )

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        finish = ""

        with self._model_slot() as slot:
            if self._cancel.is_set():
                # 等共用 model_lock 期間被取消:拿到鎖之後不得再發請求。
                raise TurnCancelled("這一輪已被使用者中斷")
            stream = self._open_stream(
                lambda: llama_client.chat_completions(
                    base_url=self.options.base_url,
                    messages=payload,
                    model=self.options.model,
                    temperature=self.options.temperature,
                    top_p=config.CHAT_TOP_P,
                    top_k=config.CHAT_TOP_K,
                    min_p=config.CHAT_MIN_P,
                    tools=self._openai_tools,
                    tool_choice="auto",
                    stream=True,
                    extra={"max_tokens": self.options.max_output_tokens},
                    timeout=self.options.request_timeout,
                ),
                slot,
            )
            protocol_error = ""
            try:
                try:
                    for chunk in _guarded(stream, self._cancel):
                        if self._cancel.is_set():
                            raise TurnCancelled("這一輪已被使用者中斷")
                        context_budget.parse_usage_from_stream_chunk(chunk, usage)
                        choices = chunk.get("choices") if isinstance(chunk, dict) else None
                        if not choices:
                            continue
                        choice = choices[0] or {}
                        delta = choice.get("delta") or {}
                        token = delta.get("content")
                        if token:
                            content_parts.append(token)
                            if on_text:
                                on_text(token)
                        thought = delta.get("reasoning_content")
                        if thought:
                            reasoning_parts.append(thought)
                            if on_reasoning:
                                on_reasoning(thought)
                        for raw in delta.get("tool_calls") or ():
                            _merge_tool_call_delta(calls, raw)
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
                except llama_client.StreamProtocolError as exc:
                    # 中間掉了一塊:之後就算收到 finish_reason=stop 也不能信。
                    protocol_error = str(exc)
                    finish = ""
            finally:
                # Ctrl-C 也要走這裡:HTTP 串流沒關掉的話 llama-server 的 slot
                # 會一直被這個請求佔著,下一個問題排在後面等到它自己結束。
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
                with self._active_lock:
                    self._active_stream = None
        if self._cancel.is_set():
            # 取消端關掉 socket 之後這裡看到的是 clean EOF:那是中斷,不是 transport 截斷。
            raise TurnCancelled("這一輪已被使用者中斷")
        context_budget.log_metrics(usage)
        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        # 沒有 finish_reason 就結束 = transport 截斷(clean EOF、缺 [DONE]、終結 chunk
        # 沒到、中間有壞掉的 SSE 行)。這不是 stop:半截的文字不是答案,半截的工具參數
        # 更不能執行。`length`(被 max_tokens 切掉)同理:可解析的 tool call 也可能是
        # 被切掉一半的參數,一律不執行。
        truncated = not finish
        cut = bool(finish) and finish not in (client_events.REASON_STOP, "tool_calls")
        if truncated:
            finish = client_events.REASON_ERROR
            calls = {}
        elif cut:
            calls = {}
        tool_calls = _finalise_tool_calls(calls)

        message: dict[str, Any] = {"role": "assistant", "content": content or None}
        if reasoning:
            message["reasoning_content"] = reasoning
        if tool_calls:
            message["tool_calls"] = [call["wire"] for call in tool_calls]
        elif (finish or client_events.REASON_STOP) != client_events.REASON_STOP or not content.strip():
            # `length`(被 max_tokens 截斷)、`error`,或 finish=stop 卻**什麼都沒回**:
            # 這一則不是完整回答。不標的話它會被狀態校正節錄當成已完成回合、
            # 也會被壓縮當成錨點。
            message["tool_status"] = client_events.STATUS_ERROR
        if tool_calls:
            # 還有工具要跑:這不是最後一則。看一次旗標再寫(檢查與 _record 不是原子:取消
            # 若插在兩者之間,這則 tool_calls 仍會記下,run_tool_loop 的 heal 會補「已中斷」
            # 的工具結果,歷史仍合法——它不是答案,不需要線性化)。
            with self._turn_state:
                if self._cancel.is_set():
                    raise TurnCancelled("這一輪已被使用者中斷")
            self._record(message)
        else:
            # 線性化點(見 _decide_commit):取消先到 → 不寫、中斷;寫定 → 之後的取消拒絕。
            self._commit_final(message)

        return {
            "content": content,
            "reasoning": reasoning,
            "tool_calls": tool_calls,
            "truncated": truncated,
            "truncation_reason": protocol_error,
            "cut": cut,
            "finish": finish or client_events.REASON_STOP,
            "tokens": {
                "input": usage.actual_prompt_eval_count or usage.estimated_input_tokens,
                "output": usage.actual_eval_count or 0,
            },
        }

    # ---- tools ---------------------------------------------------------
    def _run_one_tool(
        self,
        call: Mapping[str, Any],
        *,
        specs: Mapping[str, client_mcp.ToolSpec],
        approve: Callable[[ApprovalRequest], bool] | None,
        denied_counts: dict[str, int],
    ) -> dict[str, Any]:
        name = call["name"]
        arguments = call["arguments"]
        spec = specs.get(name)
        if spec is None:
            return self._tool_reply(
                call,
                status=client_events.STATUS_ERROR,
                text=(
                    f"status: error\nunknown tool: {name}\n"
                    "next: 只使用本輪 tool schema 列出的工具。"
                ),
            )
        if call.get("arguments_error"):
            return self._tool_reply(
                call,
                status=client_events.STATUS_ERROR,
                text=(
                    f"status: error\n{name} 的參數不是合法 JSON。\n"
                    "next: 重新送出一次結構化呼叫,參數要是合法 JSON object。"
                ),
            )

        decision = self.options.policy.decide(
            name, read_only=spec.read_only, arguments=arguments
        )
        if decision is Decision.DENY:
            return self._tool_reply(
                call,
                status=client_events.STATUS_DENIED,
                text=client_policy.denial_message(name, self.options.policy.name),
            )
        if decision is Decision.ASK:
            seen = denied_counts.get(name, 0)
            if seen >= client_policy.MAX_DENIED_RETRIES:
                # 重問有上限:同一個工具被拒絕過 N 次之後就不再打斷使用者。
                # 沒有這條的話,模型每一步重送同一個呼叫,核准框會連跳到
                # max_tool_steps 為止 —— 使用者只能一直按 n。
                return self._tool_reply(
                    call,
                    status=client_events.STATUS_DENIED,
                    text=(
                        client_policy.denial_message(name, self.options.policy.name)
                        + f"\n這個工具在這一輪已被拒絕 {seen} 次,不會再詢問使用者。"
                    ),
                )
            granted = False
            if approve is not None:
                granted = bool(
                    approve(ApprovalRequest(self.session_id, name, dict(arguments)))
                )
            if self._cancel.is_set():
                # web 的 cancel 會把等待中的核准回成「拒絕」來喚醒這裡;那不是
                # 使用者拒絕了這個工具,是整輪被中斷,不得記成一筆 denied。
                raise TurnCancelled("這一輪已被使用者中斷")
            if not granted:
                denied_counts[name] = seen + 1
                return self._tool_reply(
                    call,
                    status=client_events.STATUS_DENIED,
                    text=client_policy.denial_message(name, self.options.policy.name),
                )

        if self._cancel.is_set():
            raise TurnCancelled("這一輪已被使用者中斷")
        try:
            result = self._call_tool(name, arguments)
        except client_mcp.McpCallCancelledError:
            if self._cancel.is_set():
                raise TurnCancelled("這一輪已被使用者中斷") from None
            raise
        except client_mcp.McpClientError as exc:
            return self._tool_reply(
                call,
                status=client_events.STATUS_ERROR,
                text=f"status: error\n{exc}\nnext: 這是 MCP 傳輸層錯誤,不是工具參數問題。",
            )

        notice = client_notify.ingest_notice(name, result.text)
        return self._tool_reply(
            call,
            status=client_events.STATUS_ERROR if result.is_error else client_events.STATUS_COMPLETED,
            text=result.text,
            structured=result.structured,
            notice=notice.message if notice else "",
        )

    def _call_tool(self, name: str, arguments: Mapping[str, Any]) -> client_mcp.ToolCallResult:
        """呼叫 MCP,並把進行中的呼叫登記給 ``cancel()``。

        ``begin_call`` + ``result()`` 而不是一步的 ``call()``:後者只有
        KeyboardInterrupt 一條取消路徑,web / attach 那一端沒有 signal 可以送。
        替身 MCP(測試)沒有 ``begin_call`` 時退回 ``call()``。
        """
        begin = getattr(self.mcp, "begin_call", None)
        if not callable(begin):
            return self.mcp.call(name, arguments)
        pending = begin(name, arguments)
        with self._active_lock:
            self._active_call = pending
        try:
            if self._cancel.is_set():
                # cancel() 可能剛好在 begin_call 與登記之間跑過:它沒看到這個
                # 呼叫,這裡要自己補送取消。
                pending.cancel("cancelled by user")
            try:
                return pending.result()
            except KeyboardInterrupt:
                # 終端 REPL 的 Ctrl-C。一步式 `McpClient.call()` 會在這裡送取消;
                # 改用 begin_call 之後就要自己送——不然畫面說「已中斷」,MCP 那端
                # 的 ingest 還在寫 knowledge.json。
                pending.cancel("user interrupt")
                raise
        finally:
            with self._active_lock:
                self._active_call = None

    def _tool_reply(
        self,
        call: Mapping[str, Any],
        *,
        status: str,
        text: str,
        structured: Any = None,
        notice: str = "",
    ) -> dict[str, Any]:
        self._record(
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": text,
                "tool_status": status,
                "structured": structured,
            }
        )
        return {"status": status, "notice": notice}


# ============================================================
# streaming tool_call 組裝
# ============================================================
def _merge_tool_call_delta(calls: dict[int, dict[str, Any]], raw: Any) -> None:
    if not isinstance(raw, Mapping):
        return
    index = raw.get("index")
    if not isinstance(index, int) or isinstance(index, bool):
        # 有些 OpenAI-compatible 串流的後續 fragment 不帶 index。用 len(calls)
        # 會把一個呼叫拆成「有名稱沒參數」與「沒名稱有參數」兩個錯誤呼叫;
        # 沒有 index 的 fragment 屬於最後一個既有的 slot。
        index = max(calls) if calls else 0
    slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
    call_id = raw.get("id")
    if isinstance(call_id, str) and call_id:
        slot["id"] = call_id
    function = raw.get("function")
    if isinstance(function, Mapping):
        name = function.get("name")
        if isinstance(name, str) and name:
            slot["name"] = name
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            slot["arguments"] += arguments


def _finalise_tool_calls(calls: Mapping[int, Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for position, index in enumerate(sorted(calls)):
        slot = calls[index]
        name = slot.get("name") or ""
        raw_arguments = slot.get("arguments") or ""
        call_id = slot.get("id") or f"call_{position}"
        arguments: dict[str, Any] = {}
        arguments_error = False
        if raw_arguments.strip():
            try:
                parsed = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments_error = True
                parsed = None
            if isinstance(parsed, dict):
                arguments = parsed
            elif parsed is not None:
                arguments_error = True
        out.append(
            {
                "id": call_id,
                "name": name,
                "arguments": arguments,
                "arguments_error": arguments_error,
                "wire": {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": raw_arguments},
                },
            }
        )
    return out
