#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_engine — CodeTrail 聊天客戶端的引擎層。

負責 session 狀態、訊息組裝、工具迴圈、權限、事件流。**不負責畫面**:
終端 TUI 與 headless `run --format json` 都是這一層之上的薄前端。

四件事決定了這一層的形狀:

1. **送出去的那一份才算數。** 模型呼叫前的 payload 會先做 reasoning 剝除與
   舊工具輸出剪枝,context gate 對**剪過之後**的那一份計數,保留額用的是這
   一次 request 實送的 ``max_tokens``。session 檔與畫面保留原文。
2. **一把模型鎖。** llama-server 是單 slot;多對話、壓縮摘要與使用者訊息同時
   打模型只會排隊。鎖在這裡明講,好過在 server 端變成看不懂的等待。
3. **工具結果只有 text block 進模型。** evidence 工具的 ``structuredContent``
   是另一份完整資料,重複餵給模型等於把同一份證據算兩次 context;它只給
   UI 與 eval。
4. **預熱是零寫入的。** ``prime_prompt_cache()`` 是唯一沒有使用者訊息就打主模型
   的路徑:它只送 ``next_turn_prefix()``(與下一輪同一套轉換)、實送 ``max_tokens=1``
   且 gate 的保留額就是 1,不進 turn 狀態、不寫 session 檔、不發事件、不碰取消旗標,
   非互動 policy 一律在任何 I/O 之前拒絕。
"""
from __future__ import annotations

import contextlib
import itertools
import json
import math
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
import client_progress
import client_prompt
import config
import context_budget
import llama_client
import mcp_lease
from client_policy import Decision, PermissionPolicy

#: 一輪對話裡最多讓模型連續呼叫幾次工具。超過就停下來並講明。
DEFAULT_MAX_TOOL_STEPS = 24

#: 舊工具輸出剪枝(舊世代前端 `prune` 的等價實作,門檻逐字沿用)。
PRUNE_PROTECT_TOKENS = 40_000
PRUNE_MINIMUM_TOKENS = 20_000
PRUNE_SKIP_USER_TURNS = 2
PRUNE_PLACEHOLDER = "[Old tool result content cleared]"

#: prompt-cache 預熱(:meth:`Engine.prime_prompt_cache`)。
#: telemetry 的 source 自成一類:`.codetrail/context_metrics.jsonl` 裡的 `prime` 列
#: 不是使用者的回合,混進 `client` 會讓「這個對話問了幾次」失真。
PRIME_SOURCE = "prime"
#: 實送的 ``max_tokens``,**同時**是 gate 的保留額。1 是刻意的:預熱要的是 prefill,
#: 不是輸出;兩個數字必須相同(見 config.CLIENT_MAX_OUTPUT_TOKENS 的同一個理由)。
PRIME_MAX_TOKENS = 1
#: `/slots` probe 的上限。預熱是背景工作,不值得讓 TUI 的啟動路徑等 5 秒。
PRIME_SLOTS_TIMEOUT = 2
#: 換 session 時等進行中的預熱收工的上限(見 :meth:`Engine.abort_prime`)。
PRIME_ABORT_WAIT = 1.0

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


_INTERNAL_KEYS = frozenset({
    "time", "tool_status", "synthetic", "structured", "call_index", "queue_id", "delivery_mode",
})


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
#: 前景終端的 Ctrl-C 走的是 KeyboardInterrupt;TUI 在 worker 執行緒裡沒有 signal 可用,所以
#: 另有這條協作式路徑:串流每收到一個 chunk 就看一次旗標,進行中的 MCP 呼叫則直接走
#: client_mcp 的取消契約(notifications/cancelled → 寬限期 → SIGTERM)。
TurnCancelled = client_events.TurnCancelled


LENGTH_CUT_MESSAGE = (
    "模型的輸出被 max_tokens 切掉(finish_reason=length):這一則不完整,不會被當成答案;"
    "被切到一半的工具呼叫也不會執行。請縮小問題;輸出上限是 repo 常數 config.CLIENT_MAX_OUTPUT_TOKENS。"
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

CONVERGENCE_INSTRUCTION = (
    "本輪工具查證已到收斂邊界。這一次不可呼叫任何工具,也不可承諾稍後再查。"
    "直接針對最新真實使用者的要求,根據已回傳的證據整理答案："
    "列出已證實的具體差異與檔案:行號或來源,分清推測及尚未確認的差異,"
    "最後給一個能執行的下一步。沒有足夠證據就明說無法確定原因。"
    "工具 completed 只代表工具完成,不代表問題已解決。不要只說我理解了或找到關鍵差異,"
    "不要重問使用者已澄清的載入格式或方式,也不要捏造差異、地址、數字或引用。"
)
CONVERGENCE_NOTICE = "工具查證已到收斂邊界,正在整理已知證據、未確認項目與下一步。"
CONVERGENCE_STOP_MESSAGE = (
    "[已停止] 模型在最後一次整理時仍未形成完整答案,這一輪不再執行工具。"
    "已取得的工具結果仍保留在對話中;目前無法據此確認原因。"
    "下一步：選出仍未證實的一項差異,連同相關工具結果提出具體查證要求。"
)
UNEXECUTED_TOOL_RESULT = (
    "status: error\n這次工具呼叫未執行：本輪已進入收斂或用完工具額度。\n"
    "next: 直接整理已取得的證據、尚未確認的差異與一個具體下一步,不要再呼叫工具。"
)


def heal_in_place(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把每個懸空 tool_call 的「已中斷」結果補在**宣告它的那則訊息的群組之後**。

    順序是契約的一部分:OpenAI 形狀要求 tool 結果緊接在宣告它的 assistant
    訊息之後。補在整段尾端的話,resume 之後再送一則新問題就會排成
    `assistant(tool_calls) → user → tool`,chat template 可能直接拒收,
    也可能讓模型把結果配到錯的呼叫上。

    群組**已有**的結果排在前面、補的排在後面(宣告順序不變):同一組 `a, b` 在
    兩個工具之間被中斷時,`send()` 是由 ``heal_pending_tool_calls()`` 把 `tool(b)`
    **append** 在既有 `tool(a)` 之後;這裡若插在 `tool(a)` 之前,預熱送的 prefix
    與下一輪真的送的 payload 就在群組中途分岔 —— 兩邊都是合法歷史,prompt cache
    卻從那個 token 起一個字也重用不到,而且完全看不出來。
    """
    answered = {
        message.get("tool_call_id")
        for message in messages
        if message.get("role") == "tool"
    }
    out: list[dict[str, Any]] = [dict(message) for message in messages]
    insertions: list[tuple[int, list[dict[str, Any]]]] = []
    for index, message in enumerate(out):
        if message.get("role") != "assistant":
            continue
        healed: list[dict[str, Any]] = []
        for call in message.get("tool_calls") or ():
            if not isinstance(call, Mapping):
                continue
            call_id = call.get("id")
            if not isinstance(call_id, str) or call_id in answered:
                continue
            function = call.get("function")
            name = function.get("name") if isinstance(function, Mapping) else ""
            healed.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name if isinstance(name, str) else "",
                    "content": CANCELLED_TOOL_RESULT,
                }
            )
        if not healed:
            continue
        # 跳過這一組已經有的結果,補在它們後面。
        end = index + 1
        while end < len(out) and out[end].get("role") == "tool":
            end += 1
        insertions.append((end, healed))
    for position, healed in reversed(insertions):
        out[position:position] = healed
    return out


def heal_messages(messages: Sequence[Mapping[str, Any]]) -> int:
    """補完之後會有幾則訊息(只用來報告補了幾筆)。"""
    return len(heal_in_place(messages))


@dataclass(frozen=True)
class SessionSnapshot:
    """一次受信讀取的結果:**模型歷史與畫面歷史同源**。

    ``messages`` 是要交給模型的那一份(尊重最後一次壓縮的切點),``transcript``
    是要顯示給人看的那一份(壓縮前的原文逐字保留,壓縮本身只以一個標記呈現)。
    兩份**必須來自同一次** ``store.read()``:分兩次讀的話,另一個行程在中間
    append 的內容會讓畫面上顯示的與模型看到的不是同一段對話,而畫面上看不出來。
    """

    session_id: str
    messages: tuple[dict[str, Any], ...] = ()
    transcript: tuple[dict[str, Any], ...] = ()
    compactions: int = 0


def _compaction_summary(history: Sequence[Mapping[str, Any]]) -> str:
    """壓縮記錄裡那份摘要的內容(認不出來就回 ``""``)。

    摘要是 ``Compactor._replace`` 注入的第一則 synthetic user 訊息。認不出來時
    **不猜**:回空字串,標記照樣顯示(壓縮確實發生過),只是沒有摘要正文可展開。
    """
    if not history:
        return ""
    first = history[0]
    if first.get("role") != "user" or not first.get("synthetic"):
        return ""
    content = first.get("content")
    if not isinstance(content, str):
        return ""
    # 局部 import:摘要前綴的真值在 client_compaction(complete() 也是這樣拿的)。
    import client_compaction

    prefix = client_compaction.SUMMARY_PREFIX
    if not content.startswith(prefix):
        return ""
    return content[len(prefix):].strip()


class CancelDecision(NamedTuple):
    """``request_cancel()`` 的結果:有沒有接受,以及要在鎖外取消的 MCP 呼叫(可能 None)。"""

    accepted: bool
    call: Any


class PrimeOutcome(NamedTuple):
    """一次 :meth:`Engine.prime_prompt_cache` 的結果。**不 raise、不 print**。

    ``reason`` 的值域(``sent=True`` 時是空字串):

    ``disabled``
        ``config.CLIENT_PRIME_PROMPT_CACHE`` 是 False。
    ``tools_not_loaded``
        工具目錄還沒載入 —— prefix 少了工具 schema 就不是下一輪會送的那一份。
    ``policy``
        非互動 policy(readonly session 的評測邊界)。
    ``model_busy`` / ``turn_in_progress``
        模型鎖被別人租著 / 這個對話已經有一輪在跑。
    ``server_busy``
        `/slots` 讀得到而且**每個** slot 都在忙。
    ``next_turn_would_overflow``
        下一輪按 ``options.max_output_tokens`` 保留就會撞閘:預熱它沒有意義。
    ``gate``
        連 1 個 token 的保留額都過不了 context gate。
    ``incomplete``
        串流在終結 chunk(``finish_reason``)之前就結束了:只有 keep-alive、或送了幾個
        delta 就 clean EOF。請求沒有完成,不記 telemetry。
    ``no_timings``
        有終結 chunk,但最後一個 chunk 沒有 ``timings.prompt_n``:量不到「重算了多少」,
        不能當成功記;``usage.prompt_tokens`` 是「prompt 多長」,不拿來代填。
    ``aborted``
        ``abort_prime()``(換 session)把它收掉了。落在 `/slots` probe、取得 headers
        之前、或串流中都算;中止之後**不會**再發 POST。
    ``error:<ExcType>``
        其餘例外的型別名。

    ``processed_tokens`` 只有 ``sent=True`` 時有值,來源是
    ``ContextUsage.prompt_tokens_processed``(`timings.prompt_n`):它是唯一能分辨
    「本來就熱」與「真的搬了一次 prefill」的數字。
    """

    sent: bool
    reason: str
    processed_tokens: int | None


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

    @classmethod
    def try_lease(cls, lock: threading.Lock) -> "_ModelSlot | None":
        """非阻塞取鎖:取不到回 None(預熱用:它寧可不做,也不排隊)。

        拿到的租約不進 ``with``,只用 :meth:`release` / :meth:`hand_off` /
        :meth:`release_late`——放鎖與交給背景的規則與回合那一套完全相同。
        """
        if not lock.acquire(blocking=False):
            return None
        return cls(lock)

    def release(self) -> None:
        """正常路徑放鎖(與 ``__exit__`` 同一件事);已 ``hand_off`` 給背景就不動。"""
        self.__exit__(None, None, None)

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


def _prompt_percent(progress: Any) -> int | None:
    """Only server counts determine progress; processed already includes cache."""
    if not isinstance(progress, Mapping):
        return None
    processed, total = progress.get("processed"), progress.get("total")
    for value in (processed, total):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
    if total <= 0 or not 0 <= processed <= total:
        return None
    # Integer ratios avoid overflow and rounding an exact integer percentage down.
    numerator, denominator = processed.as_integer_ratio()
    total_numerator, total_denominator = total.as_integer_ratio()
    return (100 * numerator * total_denominator) // (denominator * total_numerator)


class _Abandonable:
    """可放棄等待的預熱 I/O；transport 的 socket 由獨立 cancellation 收掉。

    尚未排到 CPU 的 worker 先查 abort；完成得很快的 I/O 也查 abort。DNS/connect
    若晚回，transport 在送 HTTP bytes 前還會拒絕取消的 job。settle 只清理晚到資源。
    """

    def __init__(
        self, request: Callable[[], Any], name: str, *, abort: threading.Event
    ) -> None:
        self._name = name
        self._box: dict[str, Any] = {}
        self._done = threading.Event()

        def _run() -> None:
            try:
                if abort.is_set():
                    return
                self._box["value"] = request()
            except BaseException as exc:  # noqa: BLE001 - 原樣交回主流程
                self._box["error"] = exc
            finally:
                self._done.set()

        threading.Thread(target=_run, name=name, daemon=True).start()

    def wait(self, abort: threading.Event, *, poll: float = 0.05) -> bool:
        """等它完成;``abort`` 先到就回 False(結果仍會在背景到,見 :meth:`settle`)。"""
        while not abort.is_set():
            if self._done.wait(poll):
                return not abort.is_set()
            if abort.is_set():
                return False
        return False

    def result(self) -> Any:
        """完成後的結果;請求丟出的例外在這裡原樣重丟。"""
        if "error" in self._box:
            raise self._box["error"]
        return self._box.get("value")

    def settle(self, on_settled: Callable[[Any], None]) -> None:
        """放棄之後:結果到達時(可能已經到了)**恰好一次**呼叫 ``on_settled(value)``;
        請求失敗時 ``value`` 是 None。"""

        def _run() -> None:
            self._done.wait()
            on_settled(self._box.get("value"))

        if self._done.is_set():
            _run()
            return
        threading.Thread(target=_run, name=f"{self._name}-settle", daemon=True).start()


def _prime_chunk_is_final(chunk: Any) -> bool:
    """這個串流 chunk 是不是終結 chunk(與 ``parse_usage_from_stream_chunk`` 同一判準)。"""
    if not isinstance(chunk, Mapping):
        return False
    if chunk.get("stop"):
        return True
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    first = choices[0]
    return isinstance(first, Mapping) and bool(first.get("finish_reason"))


def _slot_is_busy(slot: Any) -> bool:
    """llama-server 的某個 slot 現在忙不忙。**認不出來一律當閒**。

    兩代欄位並存:新 build 給 ``is_processing``(bool),舊 build 給 ``state``
    (0 = idle)。認不出的形狀當成閒,是因為這個 probe 只用來決定「要不要順手
    預熱」—— 未知不該讓這條路徑無聲關掉(而且 4 個 slot 全忙才會跳過)。
    """
    if not isinstance(slot, Mapping):
        return False
    if slot.get("is_processing") is True:
        return True
    state = slot.get("state")
    return isinstance(state, int) and state != 0


#: 預熱 prefix 用的佔位 user 訊息標記。**永不落檔、永不送出**:它只是讓
#: reasoning 剝除與 prune 按「下一輪」的邊界計算,算完就在 to_wire 之前拿掉。
#: 用標記而不是物件 identity(`is`),是因為 strip_historical_reasoning 與
#: prune_old_tool_outputs 都會 `dict(message)` 重建每一則 —— identity 一定對不上,
#: 那樣的檢查等於沒有檢查。
_PRIME_PLACEHOLDER_KEY = "__codetrail_prime_placeholder__"


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
        if self.tool == "import_external_file":
            # 落點是工具之後才算的;只顯示 source_path 的核准等於核准一個不知道會
            # 寫到哪的動作。這裡用**同一套**檔名安全化算出目的路徑;同名時工具會
            # 加 `_N` 尾碼,所以也講明。
            import external_import

            source = str(self.arguments.get("source_path") or "")
            explicit = self.arguments.get("dest_name")
            explicit = explicit if isinstance(explicit, str) and explicit.strip() else None
            try:
                planned = external_import.planned_destination(source, explicit)
            except ValueError as exc:
                lines.append(f"  → 目的: 無法決定 —— {exc}")
            else:
                lines.append(f"  → 目的: {planned}(沙箱 root 內;同名已存在時會加 _N 尾碼)")
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
        #: 最後一次 adopt() 換上來的快照(new_session 清)。啟動時就接續好的那條路
        #: (`aicode -c` / `--session`)靠它把畫面補回來:engine 已經換過去了,
        #: 畫面還沒有那段對話,而 TUI 是在 Engine 之後才建起來的。
        self.resumed_snapshot: SessionSnapshot | None = None
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
        self._armed = False                    # 協調器在 worker 進 send() 之前就取消:下一個 turn 一開始就中斷
        # 取消的線性化鎖:_in_turn / _turn_completed / _turn_seen_since_clear / _armed 與
        # 「決定寫定」(_decide_commit)都在這把鎖裡;真正的 _record()(store append + fsync)
        # 在鎖外。request_cancel() 在鎖內原子決定接受 / 拒絕 / 武裝(見它的 docstring),
        # cancel() 與 client_turns 的協調器都走它;clear_cancel() 是協調器的回合邊界。
        # 於是取消與寫定只有兩種順序:取消先到 → 這一則不寫、以 TurnCancelled 結束;寫定先
        # 決定 → 取消拒絕。不會有「回 True 卻保留答案」或「收尾清完旗標才補設」。
        self._turn_state = threading.Lock()
        self._active_call: Any = None
        # prompt-cache 預熱(prime_prompt_cache)的狀態。**與回合狀態完全分開**:
        # 預熱不是一輪對話,不進 _begin_turn、不碰 _cancel / _armed、也不用
        # _active_stream —— 共用那些欄位的話,一次背景預熱會讓使用者按下的取消
        # 落在預熱上,或讓取消旗標留給下一題。
        self._prime_guard = threading.Lock()
        self._prime_stream: Any = None
        self._priming = False
        #: 進行中那一次預熱的中止訊號與完成訊號(沒有預熱時是 None)。每次一對新 Event,
        #: 而不是共用:共用的那個會被下一次預熱清掉,上一個等待者永遠醒不來。
        self._prime_abort: threading.Event | None = None
        self._prime_done: threading.Event | None = None
        #: 預熱的世代號:abort_prime() 與換 session 都會 +1。預熱在 snapshot 歷史時記下
        #: 當時的值,之後每個邊界(probe 回來、POST 之前、登記串流、串流結束)比一次,
        #: 不同就是「這份歷史已經不是這段對話的」→ aborted。單靠 Event 擋不住「中止先到、
        #: 預熱才登記」那一種:那時沒有 Event 可設,舊歷史照樣會送出去。
        self._prime_epoch = 0
        self._prime_request: llama_client.RequestCancellation | None = None
        self._session_switching = 0
        # UI activity is separate from persisted messages and headless events.
        self._activity_callback: Callable[[dict[str, Any]], None] | None = None
        # Only the interactive coordinator installs this. It is never consulted
        # by compaction, prime, or the headless client.
        self._supplement_callback: Callable[[], None] | None = None
        self._supplement_thread: int | None = None

    def set_supplement_callback(self, callback: Callable[[], None] | None) -> None:
        self._supplement_callback = callback

    def record_supplement(self, text: str, *, queue_id: str) -> bool:
        """Accept a real user message at the loop's model boundary.

        Admission and cancellation share the turn lock; disk I/O remains outside
        that lock. True means accepted into history, not that HTTP has completed.
        """
        if self._supplement_thread != threading.get_ident():
            raise RuntimeError("supplements are only accepted at a model-step boundary")
        if pending_tool_call_ids(self.messages):
            raise RuntimeError("cannot insert a user message inside a tool-call group")
        return self._record(
            {"role": "user", "content": text, "queue_id": queue_id,
             "delivery_mode": "supplement"},
            require_active=True,
        )

    def set_activity_callback(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        self._activity_callback = callback

    def _report_activity(
        self, operation: str, phase: str, *, percent: int | None = None, tool: str = ""
    ) -> None:
        """Publish outside cancellation locks; UI failures cannot end a turn."""
        if self._cancel.is_set():
            raise TurnCancelled("這一輪已被使用者中斷")
        callback = self._activity_callback
        if callback is not None:
            try:
                callback(client_events.activity_event(
                    self.session_id, operation=operation, phase=phase, percent=percent, tool=tool,
                ))
            except Exception:  # noqa: BLE001 - transient UI status is advisory
                pass
        # A synchronous UI bridge may accept Ctrl-C while this callback is waiting.
        if self._cancel.is_set():
            raise TurnCancelled("這一輪已被使用者中斷")

    def _activity_chunks(self, stream: Any, *, operation: str):
        """Track each request independently without changing its response chunks."""
        generating = False
        last_percent: Any = object()
        for chunk in _guarded(stream, self._cancel):
            if self._cancel.is_set():
                raise TurnCancelled("這一輪已被使用者中斷")
            if not generating and isinstance(chunk, Mapping):
                choices = chunk.get("choices")
                choice = choices[0] if isinstance(choices, list) and choices else None
                delta = choice.get("delta") if isinstance(choice, Mapping) else None
                if isinstance(delta, Mapping) and any(
                    delta.get(key) for key in ("content", "reasoning_content", "tool_calls")
                ):
                    generating = True
                    self._report_activity(operation, "generating")
                elif "prompt_progress" in chunk:
                    percent = _prompt_percent(chunk["prompt_progress"])
                    if percent != last_percent:
                        last_percent = percent
                        self._report_activity(operation, "prompt_processing", percent=percent)
            yield chunk

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
        if not self.options.keep_reasoning:
            working = strip_historical_reasoning(working)
        if self.options.prune:
            working, _pruned = prune_old_tool_outputs(working)
        return working

    def complete(self, messages: Sequence[Mapping[str, Any]], *, source: str):
        """一次非工具的模型呼叫(壓縮摘要用)。走同一個 gate 與同一把鎖。"""
        operation = "compact" if source == "compaction" else "response"
        content: list[str] = []
        reasoning: list[str] = []
        finish = ""
        try:
            self._begin_turn()
            self._report_activity(operation, "preparing")
            payload = [dict(message) for message in messages]
            usage = context_budget.check_and_log(
                source=source,
                requested_num_ctx=self.options.n_ctx,
                messages=payload,
                model=self.options.model,
                reserved_output_tokens=self.options.max_output_tokens,
                emit=False,
            )
            self._report_activity(operation, "waiting_model")
            with self._model_slot() as slot:
                # 壓縮是同一輪的尾巴:取消若落在 run_tool_loop 結束之後、摘要開始
                # 之前,這裡就要接住,不然摘要照常完成而旗標留到下一輪。
                if self._cancel.is_set():
                    raise TurnCancelled("這一輪已被使用者中斷")
                self._report_activity(operation, "waiting_response")
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
                        extra={"max_tokens": self.options.max_output_tokens, "return_progress": True},
                        timeout=self.options.request_timeout,
                    ),
                    slot,
                )
                try:
                    try:
                        for chunk in self._activity_chunks(stream, operation=operation):
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

        先關閉預熱准入並中止進行中的預熱:那一次送的是**上一段對話**的 prefix,而且它
        租著模型鎖 —— 不收掉的話,使用者在新對話問的第一題要排在一個已經沒有
        用處的請求後面。
        """
        with self._session_transition():
            session_id = self.store.create()
            # 上一段的快照不得跟過去:留著的話,啟動重播那條路會指著另一段對話。
            self._switch_session(session_id, [], None)
            return session_id

    def clear_cancel(self) -> None:
        """協調器在一輪(含尾端的壓縮)確定結束後呼叫:任何來不及消費的取消旗標都不得留到下一題。

        同時重設「這個回合裡 engine 開始過 turn」與「預先武裝」兩個閒置期狀態——它們是
        協調器回合邊界的一部分(下一個回合的真 prestart 才能再武裝)。
        """
        with self._turn_state:
            self._cancel.clear()
            self._armed = False
            self._turn_seen_since_clear = False

    def load_session(self, session_id: str) -> SessionSnapshot:
        """讀一段既有對話,**同時**產出模型歷史與畫面歷史。零狀態改動。

        模型歷史尊重最後一次壓縮的切點:不看 compaction 記錄的話,重開一個壓縮過
        的對話會把整段原始歷史再吃回 context —— 壓縮等於白做,而且第一輪就可能
        撞到 gate。畫面歷史相反:壓縮前的原文**逐字保留**(使用者要調得出當時的
        工具輸出與問答),壓縮本身只以一個標記呈現。

        兩份都從**這一次**讀取推出來,而且這裡不動 engine 任何欄位:換過去是
        :meth:`adopt` 的事。中途失敗(compaction.history 含非 dict 的項目)因此
        不可能留下「id 是新的、messages 是舊對話」的混合狀態 —— 下一題會把舊對話
        寫進新 session。
        """
        records = self.store.read(session_id)
        history: list[dict[str, Any]] = []
        transcript: list[dict[str, Any]] = []
        compactions = 0
        for index, record in enumerate(records):
            kind = record.get("type")
            if kind == "compaction":
                restored = record.get("history")
                if not isinstance(restored, list) or not all(
                    isinstance(item, Mapping) for item in restored
                ):
                    raise ValueError(f"session {session_id} 的第 {index} 筆 compaction 記錄壞掉")
                summary = _compaction_summary(restored)
                kept = len(restored) - (1 if summary else 0)
                stamp = record.get("time")
                transcript.append(
                    {
                        "type": "compaction",
                        "time": float(stamp) if isinstance(stamp, (int, float)) else 0.0,
                        "summary": summary,
                        # 壓縮掉幾則 = 當時的模型歷史長度減掉逐字保留的那幾則,
                        # 與 Compactor._replace 的 len(head) 同一個數字。
                        "dropped": max(0, len(history) - kept),
                        "kept": kept,
                    }
                )
                history = [dict(item) for item in restored]
                compactions += 1
            elif kind == "message":
                message = {k: v for k, v in record.items() if k != "type"}
                history.append(dict(message))
                transcript.append(dict(message))
        return SessionSnapshot(
            session_id=session_id,
            messages=tuple(history),
            transcript=tuple(transcript),
            compactions=compactions,
        )

    def adopt(self, snapshot: SessionSnapshot) -> None:
        """把一份已經讀好的快照換上來(原子:要嘛全換,要嘛一個欄位都沒動)。

        讀取與切換分開的理由是畫面:UI 要先拿 ``transcript`` 把 widget 建好,
        建不出來就整個不換 —— engine 換了、畫面沒換的話,使用者面對的是上一段
        對話,而模型看到的是另一段。

        與 :meth:`new_session` 同理，先關閉准入並中止上一段對話的預熱。
        """
        with self._session_transition():
            # store_error 是**這個** session 的事:上一段對話寫不進去,不代表換過來
            # 的這一段也寫不進去——不重設的話,新 session 的壓縮記錄會被靜默跳過。
            self._switch_session(
                snapshot.session_id, [dict(message) for message in snapshot.messages], snapshot
            )

    @contextlib.contextmanager
    def _session_transition(self):
        """先關預熱准入，再中止舊 job；create/adopt 失敗也一定重開准入。

        準入與 history snapshot 共用 _prime_guard，封住 abort 與狀態替換間的空窗。
        不持這把鎖等待網路或 store，也不提前替換任何 session 狀態。
        """
        with self._prime_guard:
            self._session_switching += 1
        try:
            self.abort_prime()
            yield
        finally:
            with self._prime_guard:
                self._session_switching -= 1

    def _switch_session(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        snapshot: SessionSnapshot | None,
    ) -> None:
        """換 session 的狀態切換,與預熱取歷史的那一步互斥。

        _session_transition 在整個 create/adopt 期間關閉預熱准入；狀態與世代號
        在同一臨界區替換，新准入只能取得完整的新歷史。
        """
        with self._prime_guard:
            self._prime_epoch += 1
            self.session_id = session_id
            self.messages = messages
            self.store_error = None
            self.resumed_snapshot = snapshot

    def resume(self, session_id: str) -> SessionSnapshot:
        """讀 + 換,並把快照回給呼叫端(啟動時接續的畫面要重播它)。"""
        snapshot = self.load_session(session_id)
        self.adopt(snapshot)
        return snapshot

    def _record(self, message: Mapping[str, Any], *, require_active: bool = False) -> bool:
        payload = dict(message)
        payload.setdefault("time", time.time())
        if require_active:
            with self._turn_state:
                if self._cancel.is_set() or self._in_turn == 0 or self._turn_completed:
                    return False
                self.messages.append(payload)
        else:
            self.messages.append(payload)
        try:
            self.store.append(self.session_id, {"type": "message", **payload})
        except Exception as exc:  # noqa: BLE001
            # 落檔失敗不得讓對話中斷 —— 但**一定要講**。吞掉的話這段對話只活
            # 在記憶體裡,使用者照常問下去,重開之後整段消失而且從來沒有警告。
            if self.store_error is None:
                self.store_error = f"{type(exc).__name__}: {exc}"
        return True

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
        if not self.options.keep_reasoning:
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

    def next_turn_prefix(self) -> list[dict[str, Any]]:
        """**下一輪**真的會送出去的那一份,少了最後那則使用者訊息。

        走的是與 :meth:`payload_messages` 完全同一套轉換(heal → reasoning 剝除 →
        prune),差別只有一個:先在尾端掛一則佔位 user 訊息,讓兩個轉換按「下一輪」
        的邊界算,再把它拿掉。少了這則佔位訊息,算出來的是**這一輪**的邊界——
        目前最後一則 assistant 的 reasoning 會留著、prune 少剪一筆,於是預熱送的
        prefix 跟下一輪實際要送的不是同一串 token,prompt cache 一個字也重用不到。

        因此下一次 ``send(q)`` 的 payload 恆等於 ``next_turn_prefix() + [user q]``。
        佔位訊息**永不落檔、永不送出**(見 ``_PRIME_PLACEHOLDER_KEY``)。
        """
        return self._prefix_from(self.messages)

    def _prefix_from(self, history: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """:meth:`next_turn_prefix` 的實作,吃一份 snapshot(預熱在鎖內取的那一份)。"""
        working = heal_in_place(list(history))
        working.append({"role": "user", "content": "", _PRIME_PLACEHOLDER_KEY: True})
        if not self.options.keep_reasoning:
            working = strip_historical_reasoning(working)
        if self.options.prune:
            working, _pruned = prune_old_tool_outputs(working)
        if not working or not working[-1].get(_PRIME_PLACEHOLDER_KEY):
            # 兩個轉換都不會重排也不會追加,所以這是不可能的。真的發生就是有人改了
            # 轉換的形狀:寧可 fail-loud(呼叫端把它記成 error:EngineError 並跳過這次
            # 預熱),也不要把一則內容為空的佔位 user 訊息送進模型。
            raise EngineError("預熱 prefix 的佔位訊息不在尾端;不送出可能含佔位內容的 payload")
        working.pop()
        payload = [{"role": "system", "content": self.system_prompt.text}]
        payload.extend(to_wire(working))
        return payload

    # ---- prompt cache 預熱 ---------------------------------------------
    @property
    def priming(self) -> bool:
        """現在有沒有一次預熱**真的持著模型鎖在送**(給狀態列看)。"""
        return self._priming

    def prime_prompt_cache(self, *, reason: str = "") -> "PrimeOutcome":
        """用下一輪的 prefix 送一個 ``max_tokens=1`` 的請求,把 prefill 搬到打字之前。

        這是**唯一一條沒有使用者訊息就打主模型**的路徑,所以它的邊界要逐條講明:

        - **零寫入**:不進 ``_begin_turn`` / ``_end_turn``、不 ``_record``、不發事件、
          不碰 ``_cancel`` / ``_armed`` / ``_active_stream`` / ``_active_call``,也不動
          session 檔。使用者按下的取消因此永遠落在真正的那一輪上,而不是落在一次
          背景預熱上;``request_cancel()`` 在預熱期間照樣回「閒置」。
        - **不 raise、不 print**:任何一步失敗都回 ``PrimeOutcome(False, reason, None)``。
          它是背景工作,壞掉的預熱不該變成使用者看得到的錯誤;Textual 接管畫面之後
          任何直接寫 stdout / stderr 的東西都會把畫面打花(``/slots`` 因此走
          ``quiet=True``)。
        - **讓路**:policy 不對就在任何 I/O 之前拒絕;模型鎖只非阻塞地取一次;取到
          鎖之後還要確認這個對話沒有回合在跑,而且**歷史身分取自執行當下**——排程
          當下的歷史可能已經是上一段對話了。
        - **收得掉**:``abort_prime()``(換 session)落在 `/slots` probe、POST 等 headers、
          串流中的任何一段，都 shutdown 專用 transport 的 socket，再放模型鎖。
          還在 DNS/connect 的 worker 即使晚回也不能再送 HTTP bytes；不等 headers。
        - **實送的 ``max_tokens`` 就是 gate 的保留額**(都是 ``PRIME_MAX_TOKENS``);
          另外先用下一輪的保留額 ``options.max_output_tokens`` 試算一次:下一輪反正
          會撞閘的話,預熱它沒有意義(這一次試算**不寫 log**,免得 telemetry 多出一
          列從沒發生過的請求)。

        ``reason``(``mount`` / ``new`` / ``session`` / ``compaction``)只是呼叫端的標記,
        engine 刻意**不記它**——零寫入包含 telemetry 之外的一切。
        """
        # 以下三個判定在任何 I/O、任何鎖之前:readonly session 的「不得打模型」是
        # 評測邊界,不能在 probe 之後才發現。
        if not config.CLIENT_PRIME_PROMPT_CACHE:
            return PrimeOutcome(False, "disabled", None)
        if not self._loaded_tools:
            return PrimeOutcome(False, "tools_not_loaded", None)
        if self.options.policy.name != client_policy.InteractivePolicy.name:
            # 比對的是 name 不是型別:client.json 的覆寫(OverridePolicy)沿用 base 的
            # name,所以使用者調過權限的互動 session 仍然算互動。
            return PrimeOutcome(False, "policy", None)
        slot = _ModelSlot.try_lease(self.model_lock)
        if slot is None:
            # 非阻塞:預熱寧可不做,也不能讓一個背景工作排在使用者的問題前面。
            return PrimeOutcome(False, "model_busy", None)
        done = threading.Event()
        request = llama_client.RequestCancellation()
        abort = request.event
        try:
            with self._turn_state:
                if self._in_turn > 0:
                    return PrimeOutcome(False, "turn_in_progress", None)
                with self._prime_guard:
                    if self._session_switching:
                        return PrimeOutcome(False, "aborted", None)
                    # 登記、身分、歷史與 priming 一次完成；中止不能被稍後的 snapshot 洗掉。
                    epoch = self._prime_epoch
                    history = list(self.messages)
                    self._prime_stream = None
                    self._prime_abort = abort
                    self._prime_done = done
                    self._prime_request = request
                    self._priming = True
            try:
                return self._prime_locked(abort, epoch, history, request)
            except context_budget.ContextOverflowError:
                return PrimeOutcome(False, "gate", None)
            except Exception as exc:  # noqa: BLE001 - 背景工作的失敗不得冒到呼叫端
                if abort.is_set() or self._prime_superseded(epoch):
                    return PrimeOutcome(False, "aborted", None)
                return PrimeOutcome(False, f"error:{type(exc).__name__}", None)
        finally:
            with self._prime_guard:
                stream = self._prime_stream
                self._prime_stream = None
            # 被 abort_prime() 收掉的串流在那邊就關了(它同時把登記清成 None),
            # 所以這裡永遠只會關到「自己還握著」的那一個:關一次,不是兩次。
            if stream is not None:
                _close_quietly(stream)
            # 不能只看 event：另一個取消執行緒可能尚在 shutdown。close 等它完成，
            # 並保證所有晚到的 connect 已永久失去送 HTTP 的資格，才可以放模型鎖。
            request.close()
            with self._prime_guard:
                slot.release()
                if self._prime_done is done:
                    self._priming = False
                    self._prime_done = None
                    self._prime_abort = None
                    self._prime_request = None
                done.set()

    def _prime_superseded(self, epoch: int) -> bool:
        """這次預熱取歷史之後,有沒有人(``abort_prime()`` / 換 session)宣告它作廢。"""
        with self._prime_guard:
            return self._prime_epoch != epoch

    def _prime_locked(
        self, abort: threading.Event, epoch: int,
        history: list[dict[str, Any]], request: llama_client.RequestCancellation,
    ) -> "PrimeOutcome":
        """:meth:`prime_prompt_cache` 持著模型鎖的那一段(例外由呼叫端翻成 reason)。

        每個會阻塞的 I/O 都可以被 ``abort`` 打斷、每個邊界都比一次世代號:中止落在哪一段,
        預熱就在那一段之後的第一個邊界回 ``aborted``,而且**不再發 POST**。
        """
        if abort.is_set() or self._prime_superseded(epoch):
            return PrimeOutcome(False, "aborted", None)

        probe = _Abandonable(
            lambda: llama_client.get_slots(
                self.options.base_url, timeout=PRIME_SLOTS_TIMEOUT, quiet=True, cancel=request
            ),
            "codetrail-prime-probe",
            abort=abort,
        )
        if not probe.wait(abort):
            # 專用 transport 由 finally shutdown，未完成的 probe 不會接著發 POST。
            return PrimeOutcome(False, "aborted", None)
        slots = probe.result()
        if isinstance(slots, list) and slots and all(_slot_is_busy(slot_) for slot_ in slots):
            # server 是多 slot 的:只有**每個** slot 都在忙才算滿。有一個閒著就照送
            # (server 會依最長共同前綴挑 slot);讀不到 `/slots` 視為未知,也照送。
            return PrimeOutcome(False, "server_busy", None)
        if self._prime_superseded(epoch):
            # 中止落在 probe 回來之後:什麼都還沒送,也不送。
            return PrimeOutcome(False, "aborted", None)

        payload = self._prefix_from(history)
        next_turn = context_budget.build_usage(
            source=PRIME_SOURCE,
            requested_num_ctx=self.options.n_ctx,
            messages=payload,
            tools=self._openai_tools,
            model=self.options.model,
            reserved_output_tokens=self.options.max_output_tokens,
        )
        if next_turn.hard_overflow:
            # 適用性檢查,不是這一次請求的閘:build_usage 不寫 log,telemetry 不會多
            # 出一列從來沒送出去的請求。
            return PrimeOutcome(False, "next_turn_would_overflow", None)

        usage = context_budget.check_and_log(
            source=PRIME_SOURCE,
            requested_num_ctx=self.options.n_ctx,
            messages=payload,
            tools=self._openai_tools,
            model=self.options.model,
            # 保留額 == 下面實送的 max_tokens。同一個數字才是同一個閘。
            reserved_output_tokens=PRIME_MAX_TOKENS,
            emit=False,
        )
        if self._prime_superseded(epoch):
            # 中止落在 gate 之後、POST 之前:什麼都還沒送,也不送。
            return PrimeOutcome(False, "aborted", None)
        post = _Abandonable(
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
                extra={"max_tokens": PRIME_MAX_TOKENS},
                timeout=self.options.request_timeout,
                cancel=request,
            ),
            "codetrail-prime-http",
            abort=abort,
        )
        if not post.wait(abort):
            # RequestCancellation 已有 headers 前的 socket。先 shutdown 才能放鎖；
            # 背景只收晚回資源，不再持有模型租約，也不能補發 HTTP。
            request.cancel()
            post.settle(_close_quietly)
            return PrimeOutcome(False, "aborted", None)
        stream = post.result()
        with self._prime_guard:
            superseded = abort.is_set() or self._prime_epoch != epoch
            if not superseded:
                self._prime_stream = stream
        if superseded:
            # abort_prime() 落在「請求回來、還沒登記」之間:當時沒有 socket 可關,
            # 這裡自己關掉,不留一個沒人收的串流。
            _close_quietly(stream)
            return PrimeOutcome(False, "aborted", None)

        finished = False
        try:
            for chunk in stream:
                if abort.is_set() or self._prime_superseded(epoch):
                    return PrimeOutcome(False, "aborted", None)
                context_budget.parse_usage_from_stream_chunk(chunk, usage)
                if _prime_chunk_is_final(chunk):
                    finished = True
        except Exception:  # noqa: BLE001
            # 被關掉的 socket 也可能以 I/O 例外現身:先看世代號,那是中止不是故障。
            if not self._prime_superseded(epoch):
                raise
            return PrimeOutcome(False, "aborted", None)
        if self._prime_superseded(epoch):
            # socket 被關掉之後看到的通常是 clean EOF(不是例外):那是中止,不是完成,
            # 而且**不寫 log** —— 一列半途而廢的請求會讓 T0 的判讀多出假的冷 prefix。
            return PrimeOutcome(False, "aborted", None)
        if not finished:
            # HTTP 200 但只有 keep-alive、或送了幾個 delta 就正常關線:OpenAIStream 對這種
            # EOF 不丟例外。沒有終結 chunk 就不是「正常結束」,記成 sent 會生出一列
            # prompt_tokens_processed 為空的假成功。
            return PrimeOutcome(False, "incomplete", None)
        if usage.prompt_tokens_processed is None:
            # 有終結 chunk 卻沒有 timings.prompt_n:量不到「重算了多少」。usage.prompt_tokens
            # 是 prompt 多長(cache 命中的也算),不能代填。
            return PrimeOutcome(False, "no_timings", None)
        with self._prime_guard:
            if abort.is_set() or self._prime_epoch != epoch:
                return PrimeOutcome(False, "aborted", None)
            context_budget.log_metrics(usage)
            return PrimeOutcome(True, "", usage.prompt_tokens_processed)

    def abort_prime(self, *, wait: float = PRIME_ABORT_WAIT) -> bool:
        """收掉進行中的預熱,回「它是不是已經結束」。沒有預熱時是 no-op(回 True)。

        世代號與 Event 在同一臨界區作廢；關已登記串流，並 shutdown headers 前就
        登記的 transport socket。遲到的 connect 在任何 HTTP bytes 之前被拒絕。
        然後等上限 wait 秒；done 代表 priming=False、所有舊 HTTP 已關、模型鎖已放。
        取消不碰正常回合的 _cancel / _armed。
        """
        with self._prime_guard:
            self._prime_epoch += 1
            abort = self._prime_abort
            if abort is not None:
                abort.set()
            request = self._prime_request
            stream = self._prime_stream
            # 清掉登記:關這個串流的責任在這裡,預熱那邊的 finally 就不會再關一次。
            self._prime_stream = None
            done = self._prime_done
        if stream is not None:
            _close_quietly(stream)
        if request is not None:
            request.cancel()
        if done is None:
            return True
        return done.wait(max(0.0, wait))

    # ---- one turn ------------------------------------------------------
    def send(
        self,
        text: str,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_text: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        approve: Callable[[ApprovalRequest], bool] | None = None,
        on_user_recorded: Callable[[], None] | None = None,
    ) -> TurnResult:
        emit = on_event or (lambda _event: None)
        # 旗標**不在這裡清**:協調器的 cancel 可能在 worker 還沒進到 send() 之前就到,
        # 開始時清掉就是 lost-cancel。改在這一輪收尾(_end_turn,計數歸零)時清。
        # resume 進來的歷史可能停在一個沒有結果的 tool_call(上次崩潰 / 中斷)。
        # 先補完再記新問題,順序才會是 assistant(tool_calls) → tool → user。
        # 從這裡開始就算「進行中」:寫 user 訊息(store append 可能慢)期間的 cancel() 也要算數,
        # 不然一個已經改動 session 歷史的 send() 被當成閒置,取消無效、模型請求照發。
        self._begin_turn()
        try:
            self.heal_pending_tool_calls()
            accepted = self._record(
                {"role": "user", "content": text}, require_active=on_user_recorded is not None,
            )
            if not accepted:
                raise TurnCancelled("待送訊息尚未接收,這一輪已被中斷")
            if on_user_recorded is not None:
                on_user_recorded()
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
            # 取消與 KeyboardInterrupt(headless 前景)都走這裡,而且只放棄一次。
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
                    # 協調器在 worker 進來之前就取消了:這一輪一開始就是中斷。
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
        不然協調器的鎖會跟著等 fsync。
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
        - 閒置:``arm_when_idle=False``(``cancel()``)→ 拒絕。``arm_when_idle=True``(協調器,它
          自己管理回合邊界):這個協調器回合裡 engine **已經開始過** turn(答案已寫定、或 send()
          失敗 / 中斷退出、或壓縮還沒開始)→ 拒絕——沒有東西可取消,協調器會照原結果收尾;
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
            # 這一輪結束才清旗標(見 send());協調器只在 turn 進行中才會呼叫
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
        progress = client_progress.ToolProgress(
            stagnant_steps=config.CLIENT_STAGNANT_TOOL_STEPS,
            max_entries=config.CLIENT_MAX_TOOL_CALLS_PER_TURN,
        )
        converge = False
        preambles: set[str] = set()

        while steps < self.options.max_tool_steps:
            if self._cancel.is_set():
                raise TurnCancelled("這一輪已被使用者中斷")
            # The previous iteration has recorded every result in the declared
            # tool group. Never poll while HTTP, a tool, or approval is in flight.
            if self._supplement_callback is not None:
                self._supplement_thread = threading.get_ident()
                try:
                    self._supplement_callback()
                finally:
                    self._supplement_thread = None
            if self._cancel.is_set():
                raise TurnCancelled("這一輪已被使用者中斷")
            # 已有工具步才預留最後一個模型步驟;max=1 保留原本可執行工具的語意。
            final_pass = converge or (steps > 0 and steps == self.options.max_tool_steps - 1)
            if final_pass:
                on_event(client_events.notice_event(self.session_id, CONVERGENCE_NOTICE))
            steps += 1
            step = self._one_model_step(
                on_text=on_text, on_reasoning=on_reasoning,
                tool_choice="none" if final_pass else "auto",
                prior_preambles=preambles,
            )
            final_text = step["content"] or final_text
            if step["content"]:
                on_event(client_events.text_event(self.session_id, step["content"]))

            calls = step["tool_calls"]
            if not calls:
                finish = step["finish"] or client_events.REASON_STOP
                if step.get("convergence_failed"):
                    finish = client_events.REASON_ERROR
                    final_text = CONVERGENCE_STOP_MESSAGE
                    self._commit_final({
                        "role": "assistant", "content": final_text,
                        "tool_status": client_events.STATUS_ERROR,
                    })
                    on_event(client_events.text_event(self.session_id, final_text))
                elif step.get("truncated"):
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

            if step["content"]:
                preambles.add(" ".join(step["content"].split()))
            on_event(
                client_events.step_finish_event(
                    self.session_id,
                    reason=client_events.REASON_TOOL_CALLS,
                    tokens=step["tokens"],
                )
            )
            progress.begin_step()
            for call in calls:
                if self._cancel.is_set():
                    raise TurnCancelled("這一輪已被使用者中斷")
                tool_calls += 1
                # server 不遵守 tool_choice=none 也不能真的執行;同批超額宣告仍須
                # 逐條補結果,保持 assistant(tool_calls) → tools 的完整相鄰順序。
                if final_pass or tool_calls > config.CLIENT_MAX_TOOL_CALLS_PER_TURN:
                    outcome = self._tool_reply(
                        call, status=client_events.STATUS_ERROR, text=UNEXECUTED_TOOL_RESULT,
                    )
                else:
                    outcome = self._run_one_tool(
                        call, specs=specs, approve=approve, denied_counts=denied_counts
                    )
                spec = specs.get(call["name"])
                progress.observe(
                    call["name"], call["arguments"], outcome["text"],
                    status=outcome["status"],
                    read_only=spec is not None and spec.read_only is True,
                    dispatched=outcome["dispatched"],
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
            if final_pass:
                finish = client_events.REASON_ERROR
                final_text = CONVERGENCE_STOP_MESSAGE
                self._commit_final({
                    "role": "assistant", "content": final_text,
                    "tool_status": client_events.STATUS_ERROR,
                })
                on_event(client_events.text_event(self.session_id, final_text))
                on_event(client_events.step_finish_event(self.session_id, reason=finish))
                break
            converge = progress.finish_step() or tool_calls >= config.CLIENT_MAX_TOOL_CALLS_PER_TURN
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
        tool_choice: str = "auto",
        prior_preambles: set[str] | None = None,
    ) -> dict[str, Any]:
        self._report_activity("response", "preparing")
        payload, transform = self.payload_messages()
        if tool_choice == "none":
            # 只改局部 wire payload,先加指示再 gate;session、預熱 prefix 與下一輪
            # 的 system prompt 都保持原文。tools 保留,HTTP adapter 才會實送 none。
            payload[0] = {**payload[0], "content": payload[0]["content"] + "\n\n" + CONVERGENCE_INSTRUCTION}
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

        self._report_activity("response", "waiting_model")
        with self._model_slot() as slot:
            if self._cancel.is_set():
                # 等共用 model_lock 期間被取消:拿到鎖之後不得再發請求。
                raise TurnCancelled("這一輪已被使用者中斷")
            self._report_activity("response", "waiting_response")
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
                    tool_choice=tool_choice,
                    stream=True,
                    extra={"max_tokens": self.options.max_output_tokens, "return_progress": True},
                    timeout=self.options.request_timeout,
                ),
                slot,
            )
            protocol_error = ""
            try:
                try:
                    for chunk in self._activity_chunks(stream, operation="response"):
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
        repeated_preamble = bool(
            tool_choice == "none" and content.strip()
            and " ".join(content.split()) in (prior_preambles or ())
        )
        convergence_failed = tool_choice == "none" and (
            truncated or cut or not content.strip() or repeated_preamble
        )
        if repeated_preamble:
            finish = client_events.REASON_ERROR

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
        if tool_calls or convergence_failed:
            # 還有工具要跑:這不是最後一則。看一次旗標再寫(檢查與 _record 不是原子:取消
            # 若插在兩者之間,這則 tool_calls 仍會記下,run_tool_loop 的 heal 會補「已中斷」
            # 的工具結果,歷史仍合法——它不是答案,不需要線性化)。收斂失敗時同樣
            # 保留原始錯誤輸出,最後的明確停止訊息由 loop 經 _commit_final 寫入。
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
            "convergence_failed": convergence_failed,
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
                self._report_activity("response", "approval", tool=name)
                granted = bool(
                    approve(ApprovalRequest(self.session_id, name, dict(arguments)))
                )
            if self._cancel.is_set():
                # 協調器的 cancel 會把等待中的核准回成「拒絕」來喚醒這裡;那不是
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
        self._report_activity("response", "tool", tool=name)
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
                dispatched=True,
            )

        notice = client_notify.ingest_notice(name, result.text)
        return self._tool_reply(
            call,
            status=client_events.STATUS_ERROR if result.is_error else client_events.STATUS_COMPLETED,
            text=result.text,
            structured=result.structured,
            notice=notice.message if notice else "",
            dispatched=True,
        )

    def _call_tool(self, name: str, arguments: Mapping[str, Any]) -> client_mcp.ToolCallResult:
        """呼叫 MCP,並把進行中的呼叫登記給 ``cancel()``。

        ``begin_call`` + ``result()`` 而不是一步的 ``call()``:後者只有
        KeyboardInterrupt 一條取消路徑,TUI 的 worker 執行緒沒有 signal 可以送。
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
                # headless 前景的 Ctrl-C。一步式 `McpClient.call()` 會在這裡送取消;
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
        dispatched: bool = False,
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
        return {"status": status, "notice": notice, "text": text, "dispatched": dispatched}


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


#: server 沒給 tool-call id 時的退回編號。**全行程單調遞增**,不是「這個 step 的
#: 第幾個」:後者讓每個 step 的第一個工具都叫 `call_0`,而 TUI 以 id 當全域 key,
#: 第二次呼叫就掛在第一次的 block 上。
_FALLBACK_CALL_IDS = itertools.count(1)


def _finalise_tool_calls(calls: Mapping[int, Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index in sorted(calls):
        slot = calls[index]
        name = slot.get("name") or ""
        raw_arguments = slot.get("arguments") or ""
        call_id = slot.get("id") or f"call_{next(_FALLBACK_CALL_IDS)}"
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
