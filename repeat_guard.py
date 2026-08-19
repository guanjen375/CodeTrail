#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同參數重複呼叫偵測 — 打斷小模型的搜尋鬼打牆。

觀察到的失敗模式(2026-08-19):本機小模型在 OpenCode 裡對同一組
grep_code 參數連續呼叫五、六次,每次 thinking 40–70 秒,結果一模一樣
卻停不下來。工具端無法修模型,但可以改變它看到的內容:當「同工具、
同參數、同結果」連續發生時,在結果前面加一段醒目的打斷文字,明確指示
不要再重複、該做什麼。結果內容變了(檔案真的被改過)計數就歸零,
所以這個機制永遠不會遮蔽新資訊 — 每次都重新執行工具、重新比對結果。

只掛在唯讀查詢工具上(grep_code / read_file / ...):寫入與執行類工具
的重複呼叫是合法工作流(改→測→改→測),不打斷。

狀態存在 server 行程記憶體(per-session),LRU 上限防止無限成長。
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict

# 第幾次「同參數同結果」開始加打斷文字(2 = 第一次重複就打斷)。
BANNER_THRESHOLD = 2

# LRU 追蹤的 (tool, args) 鍵數上限。
MAX_TRACKED_KEYS = 100


class RepeatGuard:
    """追蹤 (tool, args) → (結果簽章, 連續相同次數)。

    observe() 回傳「連續拿到相同結果的次數」(第一次 = 1)。
    結果簽章一變就重新從 1 起算,所以檔案被改過之後的下一次呼叫
    永遠不會被標成重複。
    """

    def __init__(self, max_keys: int = MAX_TRACKED_KEYS):
        self.max_keys = max_keys
        self._seen: OrderedDict[tuple, tuple[str, int]] = OrderedDict()

    def observe(self, tool: str, args_key: str, result_text: str) -> int:
        sig = hashlib.sha1(
            result_text.encode("utf-8", errors="replace")
        ).hexdigest()
        key = (tool, args_key)
        prev = self._seen.get(key)
        count = prev[1] + 1 if prev is not None and prev[0] == sig else 1
        self._seen[key] = (sig, count)
        self._seen.move_to_end(key)
        while len(self._seen) > self.max_keys:
            self._seen.popitem(last=False)
        return count


def args_key(args: tuple, kwargs: dict) -> str:
    """呼叫參數的 canonical 字串(dict 順序不影響)。"""
    try:
        return json.dumps(
            [list(args), kwargs], sort_keys=True, ensure_ascii=False, default=repr
        )
    except Exception:
        return repr((args, kwargs))


def banner(tool: str, count: int) -> str:
    """打斷文字。放在工具結果最前面,讓模型第一眼就看到。"""
    return (
        f"⚠ [重複呼叫偵測] 這是你第 {count} 次用完全相同的參數呼叫 {tool},"
        "而且結果與上次完全相同(相關檔案沒有變化)。\n"
        "不要再重複這個呼叫 — 再呼叫只會得到一模一樣的輸出。\n"
        "請二選一:(1) 直接根據下方既有結果做下一步;"
        "(2) 換不同的 pattern / path / 工具再查。\n\n"
    )
