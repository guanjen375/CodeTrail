#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_notify — 兩個從舊前端的 notify plugin 搬進客戶端的通知機制。

1. **ingest 待辦通知**:``ingest_document`` 的結果裡出現行首的
   ``[CODETRAIL_ACTION_REQUIRED]`` / ``[CODETRAIL_INGEST_FAILED]`` 時,提示
   使用者一次(每個 call 一次)。
2. **假工具呼叫偵測**:助理以純文字宣稱「我現在呼叫工具」卻沒有任何結構化
   tool call 時,提示一次並寫一筆零內容的 incident。

兩條的判準都刻意保守(寧可漏報):
  * marker **只認行首**。用 ``in`` 判的話,工具結果裡那條「可以直接複製的
    CLI 命令」會誤觸 —— 檔名可以合法地叫 ``[CODETRAIL_ACTION_REQUIRED].pdf``,
    而那條命令必須逐字保留檔名。真正的待辦區塊自己起一行。
  * marker **只認 ``ingest_document`` 的結果**。任何工具都能回傳含 marker 的
    文字(``read_file`` 讀到一份講 marker 的文件就會),不限定工具名等於讓
    任何檔案內容偽造一則待辦。
  * 「宣稱呼叫工具」只認明確的宣告句,而且動詞與「工具」之間不得出現否定詞。
    用「工具」「tool」當標記會讓「我沒有呼叫任何工具」也命中。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ingest_notify import ACTION_REQUIRED_MARKER, FAILED_MARKER

#: 只有這個工具的結果會被信任來宣告待辦。
ACTION_MARKER_TOOL = "ingest_document"

#: 「宣稱呼叫工具」只掃回覆開頭這麼多字元。
MAX_TEXT_SCAN = 20_000

#: 判準與舊 JS plugin 的 CLAIM_PATTERNS 相同(那份現在只是 inert stub)。
CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"<tool_call>", re.IGNORECASE),
    re.compile(r'"name"\s*:\s*"codetrail_'),
    # 動詞與「工具」之間不得出現否定詞:少了它,「我會使用文字說明,而不呼叫
    # 任何工具」會被判成宣稱呼叫工具。
    re.compile(
        r"(?:我|讓我)(?:現在|接下來|馬上|先)?(?:就)?(?:來|會|將|要)?"
        r"(?:呼叫|使用|調用|叫用|執行)(?:(?!不|沒|無|非|別|勿|未)[^。\n]){0,40}?(?:工具|tool)"
    ),
    re.compile(
        r"\bI(?:'|’)?(?:ll| will| am going to| am about to)\s+(?:now\s+)?"
        r"(?:call|use|invoke|run)"
        r"(?!\s+(?:out|off|on|for|upon|into|back|around|over|through|no|not|never|nothing)\b)"
        r"(?!\s+(?:this|that|it)\s+an?\b)"
        r"(?:(?!\bwithout\b|\bno\b|\bnot\b|\bnever\b)[^.\n]){0,80}?\btool\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\blet me\s+(?:now\s+)?(?:call|use|invoke|run)"
        r"(?!\s+(?:out|off|on|for|upon|into|back|around|over|through|no|not|never|nothing)\b)"
        r"(?!\s+(?:this|that|it)\s+an?\b)"
        r"(?:(?!\bwithout\b|\bno\b|\bnot\b|\bnever\b)[^.\n]){0,80}?\btool\b",
        re.IGNORECASE,
    ),
)


def text_has_marker(value: object, marker: str) -> bool:
    """marker 只認行首(見 module docstring)。"""
    if not isinstance(value, str) or marker not in value:
        return False
    return any(line.lstrip().startswith(marker) for line in value.split("\n"))


@dataclass(frozen=True)
class IngestNotice:
    kind: str  # "action_required" | "ingest_failed"
    message: str


_ACTION_MESSAGE = (
    "這次 ingest 有需要人工處理的圖面。請照結果裡 "
    f"{ACTION_REQUIRED_MARKER} 區塊列出的 figure 與下一步處理,不要當成已完成。"
)
_FAILED_MESSAGE = (
    "這次 ingest 有抽取失敗的項目。請照結果裡 "
    f"{FAILED_MARKER} 區塊列出的項目決定接受或重灌。"
)


def ingest_notice(tool_name: str, result_text: object) -> IngestNotice | None:
    """只對 ``ingest_document`` 的結果判定待辦。每個 call 呼叫一次。

    **失敗優先。** 兩個 marker 同時出現時,`ingest_notify.classify_ingest_body`
    把整次結果判成 error;這裡若回「有待覆核的圖」,使用者會被導去處理一份
    根本沒有成功入庫的文件。
    """
    if tool_name != ACTION_MARKER_TOOL:
        return None
    if text_has_marker(result_text, FAILED_MARKER):
        return IngestNotice("ingest_failed", _FAILED_MESSAGE)
    if text_has_marker(result_text, ACTION_REQUIRED_MARKER):
        return IngestNotice("action_required", _ACTION_MESSAGE)
    return None


def claims_tool_call(text: object) -> bool:
    """助理是不是用純文字宣稱它呼叫了工具(保守判準)。"""
    if not isinstance(text, str) or not text:
        return False
    head = text[:MAX_TEXT_SCAN]
    return any(pattern.search(head) for pattern in CLAIM_PATTERNS)


PROMISE_WITHOUT_CALL_MESSAGE = (
    "助理宣稱要呼叫工具,但這一輪沒有任何結構化 tool call。"
    "純文字 / XML 不是工具呼叫;請直接重問一次,或明確點名要用哪個工具。"
)
