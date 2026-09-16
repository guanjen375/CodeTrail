#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_events — CodeTrail 客戶端的事件流格式與**唯一**解析器。

三個下游共用同一份格式與同一個解析器:
  - `scripts/tool_call_canary.py` 的 explicit / implicit lane
  - `scripts/eval_tool_routing.py` 的 routing eval
  - `scripts/session_eval.py` 的 read-only replay

各自寫一份解析器的代價是靜默的:某一端多認一種形狀時,同一次 run 在兩邊會
得到不同判定,而兩邊都不會報錯。

事件是 JSONL,一行一個 JSON object。形狀刻意保持保守且穩定:
    {"type": "session",     "sessionID": ..., "root": ..., "model": ...}
    {"type": "text",        "sessionID": ..., "part": {"type": "text", "text": ...}}
    {"type": "tool_use",    "sessionID": ..., "part": {"type": "tool", "tool": ...,
                            "callID": ..., "state": {"status": ..., "input": {...}}}}
    {"type": "step_finish", "sessionID": ..., "part": {"type": "step-finish",
                            "reason": ..., "tokens": {...}}}
    {"type": "compaction",  "sessionID": ..., "part": {"type": "compaction", ...}}
    {"type": "error",       "sessionID": ..., "message": ...}

**工具名是裸名**(`list_dir`,不是 `codetrail_list_dir`)。

事件流帶精確的 session id,好讓 canary / eval 之後刪掉自己產生的對話。
`reasoning` 不進事件流:它只用於終端串流顯示,寫進 JSONL 等於把 thinking
逐字落到任何收集這份輸出的地方。
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SCHEMA = 1

TYPE_SESSION = "session"
TYPE_TEXT = "text"
TYPE_TOOL_USE = "tool_use"
TYPE_STEP_FINISH = "step_finish"
TYPE_COMPACTION = "compaction"
TYPE_ERROR = "error"
#: 給使用者看的提醒(ingest 待辦、假工具呼叫、對話未落檔、壓縮結果)。
#: 它不是模型輸出,所以不進 assistant_text,也不影響 terminal 判定。
TYPE_NOTICE = "notice"
#: 串流中的文字片段。只給 UI;`text` 事件才是這一輪的完整文字。
TYPE_TEXT_DELTA = "text_delta"
#: 等待階段與 prompt 進度。只經 TUI 的活動回呼,不進 headless JSONL、session
#: 或模型歷史;固定階段／數值／工具名以外不攜帶 prompt 或模型輸出。
TYPE_ACTIVITY = "activity"
# Local TUI queue state. This is neither assistant output nor a terminal event.
TYPE_QUEUE = "message_queue"

STATUS_COMPLETED = "completed"
STATUS_ERROR = "error"
STATUS_DENIED = "denied"

REASON_STOP = "stop"
REASON_TOOL_CALLS = "tool-calls"
REASON_ERROR = "error"
#: 使用者中斷(TUI 的 Ctrl-C,經 client_turns 的協調器)。是終結事件:看終結
#: 事件收工的一端要能收工,但它不是答案,所以 idle 的壓縮不會接在它後面跑。
REASON_CANCELLED = "cancelled"

class TurnCancelled(RuntimeError):
    """這一輪被使用者中斷(``Engine.cancel()``)。

    定義在事件模組是為了讓 `client_compaction` 認得出它而不 import engine:
    壓縮期間被中斷不是「摘要不可信」,不得記成永久停用。
    """


SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")

#: 工具結果裡不得原樣出現在助理文字中的內部 marker(洩漏偵測)。
MARKER_LEAK_TOKENS = ("[CODETRAIL_INGEST_SUMMARY]",)


# ============================================================
# emit
# ============================================================
def session_event(session_id: str, *, root: str, model: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "type": TYPE_SESSION,
        "sessionID": session_id,
        "root": root,
        "model": model,
    }


def text_event(session_id: str, text: str) -> dict[str, Any]:
    return {
        "type": TYPE_TEXT,
        "sessionID": session_id,
        "part": {"type": "text", "text": text},
    }


def queue_event(session_id: str, item: Mapping[str, Any]) -> dict[str, Any]:
    """A local input's state; only delivered items carry their accepted text."""
    part = dict(item)
    part["type"] = "message-queue"
    if part.get("status") != "delivered":
        part.pop("text", None)
    return {"type": TYPE_QUEUE, "sessionID": session_id, "part": part}


def tool_event(
    session_id: str,
    *,
    tool: str,
    call_id: str,
    status: str,
    arguments: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": TYPE_TOOL_USE,
        "sessionID": session_id,
        "part": {
            "type": "tool",
            "tool": tool,
            "callID": call_id,
            "state": {"status": status, "input": dict(arguments or {})},
        },
    }


def step_finish_event(
    session_id: str,
    *,
    reason: str,
    tokens: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    part: dict[str, Any] = {"type": "step-finish", "reason": reason}
    if tokens:
        part["tokens"] = dict(tokens)
    return {"type": TYPE_STEP_FINISH, "sessionID": session_id, "part": part}


def compaction_event(session_id: str, *, status: str, detail: str = "") -> dict[str, Any]:
    return {
        "type": TYPE_COMPACTION,
        "sessionID": session_id,
        "part": {"type": "compaction", "status": status, "detail": detail},
    }


def error_event(session_id: str, message: str) -> dict[str, Any]:
    return {"type": TYPE_ERROR, "sessionID": session_id, "message": message}


def text_delta_event(session_id: str, chunk: str) -> dict[str, Any]:
    """串流片段。**不進 assistant_text**、不影響 terminal 判定 —— 解析器只認
    `text` 事件,所以加這個不會讓 canary / eval 看到同一段文字兩次。"""
    return {"type": TYPE_TEXT_DELTA, "sessionID": session_id, "part": {"text": chunk}}


def activity_event(
    session_id: str,
    *,
    operation: str,
    phase: str,
    percent: int | None = None,
    tool: str = "",
) -> dict[str, Any]:
    """UI 活動資料;百分比只代表本次 prompt 已處理的 token 比例。"""
    part: dict[str, Any] = {"operation": operation, "phase": phase}
    if percent is not None:
        part["percent"] = percent
    if tool:
        part["tool"] = tool
    return {"type": TYPE_ACTIVITY, "sessionID": session_id, "part": part}


def notice_event(session_id: str, message: str) -> dict[str, Any]:
    """`TurnResult.notices` 與壓縮結果的事件形狀。

    TUI 把這些字顯示成提示行;沒有它就等於 ingest 的
    ``[CODETRAIL_ACTION_REQUIRED]``、假工具呼叫警告與「這段對話沒有落檔」
    全部靜默消失。
    """
    return {"type": TYPE_NOTICE, "sessionID": session_id, "message": message}


def dumps(event: Mapping[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False)


# ============================================================
# parse
# ============================================================
@dataclass(frozen=True)
class CompletedToolCall:
    tool: str
    arguments: dict[str, Any] | None

    @property
    def bare_tool(self) -> str:
        """歷史相容:舊前端會加 `codetrail_` 前綴,自家事件流不會。"""
        return self.tool.split("_", 1)[1] if self.tool.startswith("codetrail_") else self.tool

    @property
    def identity(self) -> str:
        payload = {"tool": self.bare_tool, "arguments": self.arguments}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def event_part(event: Mapping[str, Any]) -> Mapping[str, Any]:
    part = event.get("part")
    return part if isinstance(part, Mapping) else {}


def safe_nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def event_session_ids(event: Mapping[str, Any]) -> list[str]:
    part = event_part(event)
    values = (
        event.get("sessionID"),
        event.get("session_id"),
        part.get("sessionID"),
        part.get("session_id"),
    )
    return [value for value in values if isinstance(value, str) and SAFE_SESSION_ID_RE.fullmatch(value)]


def event_generated_text(event: Mapping[str, Any]) -> str:
    """取助理產生的文字,**絕不**取工具的 state.output。"""
    event_type = str(event.get("type", ""))
    part = event_part(event)
    part_type = str(part.get("type", ""))
    if event_type == "reasoning" or part_type == "reasoning":
        return ""
    if event_type not in ("text", "assistant_text") and part_type not in ("text", "assistant-text"):
        return ""
    for container in (part, event):
        for key in ("text", "content"):
            value = container.get(key)
            if isinstance(value, str):
                return value
    return ""


def completed_tool_call(event: Mapping[str, Any]) -> CompletedToolCall | None:
    part = event_part(event)
    event_type = str(event.get("type", ""))
    part_type = str(part.get("type", ""))
    if event_type not in ("tool_use", "tool-use") and part_type not in ("tool", "tool-use"):
        return None
    state_value = part.get("state", event.get("state"))
    state = state_value if isinstance(state_value, Mapping) else {}
    if state.get("status") != STATUS_COMPLETED:
        return None
    tool = part.get("tool", event.get("tool"))
    if not isinstance(tool, str) or not tool:
        return None
    raw_arguments = state.get("input", part.get("input", event.get("input")))
    arguments = dict(raw_arguments) if isinstance(raw_arguments, Mapping) else None
    return CompletedToolCall(tool, arguments)


def is_terminal_event(event: Mapping[str, Any]) -> bool:
    """助理是不是「這一輪真的答完了」。

    `tool-calls` 的 step 是中間邊界:成功必須看到之後那一則終止 step,
    純文字 / XML 因此無法冒充一次工具呼叫。
    """
    part = event_part(event)
    event_type = str(event.get("type", "")).replace("-", "_")
    part_type = str(part.get("type", "")).replace("-", "_")
    if event_type not in ("step_finish", "message_finish", "session_finish") and part_type not in (
        "step_finish",
        "message_finish",
        "session_finish",
    ):
        return False
    reason = part.get("reason", event.get("reason"))
    return reason not in ("tool-calls", "tool_calls")


def token_values(event: Mapping[str, Any]) -> tuple[int, int, int, int, int]:
    part = event_part(event)
    tokens_value = part.get("tokens", event.get("tokens"))
    if not isinstance(tokens_value, Mapping):
        return (0, 0, 0, 0, 0)
    prompt = safe_nonnegative_int(
        tokens_value.get("input", tokens_value.get("prompt", tokens_value.get("prompt_tokens")))
    )
    output = safe_nonnegative_int(
        tokens_value.get("output", tokens_value.get("completion", tokens_value.get("output_tokens")))
    )
    reasoning = safe_nonnegative_int(tokens_value.get("reasoning"))
    cache_value = tokens_value.get("cache")
    if isinstance(cache_value, Mapping):
        cache = sum(
            safe_nonnegative_int(cache_value.get(key)) for key in ("read", "write", "input", "output")
        )
    else:
        cache = safe_nonnegative_int(cache_value)
    total = safe_nonnegative_int(tokens_value.get("total"))
    if total == 0:
        total = prompt + output + reasoning + cache
    return prompt, output, reasoning, cache, total


def iter_events(output: str):
    """逐行解析 JSONL;壞行略過(部分輸出仍要能判讀)。"""
    for line in (output or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event
