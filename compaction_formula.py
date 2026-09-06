#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""compaction_formula — 壓縮門檻的推導公式與 canonical 規則文字。

客戶端(`client_compaction`)、`config` 的 ``CLIENT_MAX_OUTPUT_TOKENS`` 上限檢查、
`set_config` 的摘要頁與 `scripts/session_eval.py` 的指紋都吃這裡。

**數值不是拍腦袋的百分比**:門檻與 tail 保留額沿用 2026-09 定案的公式
(當初的推導來源見 git 歷史),加上 CodeTrail 自己的單次工具結果預算算出來
(見 :func:`derive_settings`)。改公式就會改變「什麼時候壓縮」與「壓完留多少」,
所以它只有這一份。
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent

#: 七條摘要規則的 canonical 文件。摘要格式核對的欄位標題從這裡解析出來。
RULES_DOC = REPO_ROOT / "docs" / "compaction-rules.md"

# ---- 推導常數 ------------------------------------------------------------
#: 沒有明寫 ``compaction.reserved`` 時的保留額上限。
COMPACTION_RESERVE_TOKENS = 20_000
#: 有效輸出額的 cap:``min(limit.output, 32000) || 32000``。不套這個 cap 的話,
#: 對 ``limit.output`` 很大的模型會算出與公式不同的門檻。
OUTPUT_TOKEN_MAX = 32_000
#: 門檻與 tail 上限的下界:低於這個數字就推不出可用的壓縮設定。
MIN_PRESERVE_RECENT_TOKENS = 2_000
#: CodeTrail 單次工具結果的 context 佔比(tool_result_adapter.DEFAULT_CONTEXT_FRACTION)。
#: 這裡刻意重寫一份字面值而不是 import:tool_result_adapter 會拉進 MCP server 端
#: 的相依,而這個模組要能被 set_config / contract check 這種純 stdlib 路徑載入。
#: 兩邊不同步由 tests 的一致性檢查抓。
TOOL_RESULT_CONTEXT_FRACTION = 0.12
#: CodeTrail / manual 模式固定保留一輪 recent tail。
#: 為什麼是 1:tail 之外的東西才會進摘要器,而 idle 觸發時「最後一輪」正好是
#: 剛答完的那一輪(競態時則是還沒被回答的那則使用者訊息)。多留幾輪只會讓
#: 壓縮後可用空間變小,擋不住任何額外的失真。
TAIL_TURNS = 1


class CompactionModeError(RuntimeError):
    """模式或受管值無法安全推導 —— 不猜,直接說。"""


# ---------------------------------------------------------------------------
# canonical 規則文字
# ---------------------------------------------------------------------------
#: `docs/compaction-rules.md` 裡兩個 ```text 區塊各自的第一行(區塊的身分)。
RULES_BLOCK_MARKER = "[CodeTrail 壓縮規則]"
RECONCILIATION_BLOCK_MARKER = "[CodeTrail 狀態校正]"

_FENCE = "```text"


def doc_text_blocks(text: str) -> list[str]:
    """抓出 markdown 裡所有 ```text 圍籬區塊的內容(不含圍籬本身)。"""
    blocks: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].strip() == _FENCE:
            index += 1
            body: list[str] = []
            while index < len(lines) and lines[index].strip() != "```":
                body.append(lines[index])
                index += 1
            blocks.append("\n".join(body))
        index += 1
    return blocks


def canonical_block(marker: str, doc: Path | None = None) -> str:
    """回傳 canonical 文件裡以 ``marker`` 起首的那個 ```text 區塊。

    找不到就 raise —— 送進摘要器的規則字面值與文件不一致是靜默的失真來源
    (模型照舊產出摘要,只是規則換了一份),不能靜靜退回空字串。
    """
    target = RULES_DOC if doc is None else doc
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise CompactionModeError(f"讀不到壓縮規則文件 {target}:{exc}") from exc
    matches = [block for block in doc_text_blocks(text) if block.startswith(marker)]
    if len(matches) != 1:
        raise CompactionModeError(
            f"{target} 裡以 {marker!r} 起首的 ```text 區塊有 {len(matches)} 個,必須剛好 1 個"
        )
    return matches[0]


#: 規則 1 規定的固定欄位數。改這個數字之前先改文件 —— 兩邊不一致時
#: `rule_headings()` 會 fail-loud,不會回一個少一欄的清單。
RULE_HEADING_COUNT = 7


def rule_headings(doc: Path | None = None) -> tuple[str, ...]:
    """規則 1 規定的七個固定欄位標題,**從 canonical 區塊解析出來**(順序即契約)。

    為什麼用解析而不是再抄一份字面值:壓縮後的格式核對要拿這七個標題去比對
    模型產出的摘要。抄一份的話,改了文件卻沒改這裡,核對就會用舊標題去驗新
    規則 —— 而合法的摘要會被判成漂移、漂移的摘要會被放行,兩種都是靜默的。
    """
    block = canonical_block(RULES_BLOCK_MARKER, doc)
    start = block.find("\n1. ")
    end = block.find("\n2. ", start + 1) if start >= 0 else -1
    if start < 0 or end < 0:
        raise CompactionModeError(
            "壓縮規則區塊裡找不到第 1 條(固定欄位)的範圍,無法解析七個標題"
        )
    names = tuple(re.findall(r"##\s*([^\s、。]+)", block[start:end]))
    if len(names) != RULE_HEADING_COUNT:
        raise CompactionModeError(
            f"壓縮規則第 1 條解析出 {len(names)} 個標題,應該剛好 "
            f"{RULE_HEADING_COUNT} 個:{names}"
        )
    return names

# ---------------------------------------------------------------------------
# 推導
# ---------------------------------------------------------------------------
class DerivedSettings:
    """一個模型的壓縮門檻與 tail 保留額。

    runtime 只吃 ``idle_threshold``(什麼時候壓)與 ``preserve_recent_tokens``
    (壓完留多少)。
    """

    __slots__ = (
        "context_limit",
        "output_limit",
        "input_limit",
        "reserved",
        "usable",
        "tool_result_budget",
        "headroom",
        "tail_cap",
        "preserve_recent_tokens",
        "idle_threshold",
    )

    def __init__(self, **values: Any) -> None:
        for name in self.__slots__:
            setattr(self, name, values[name])

    @property
    def tail_holds_a_full_headroom_turn(self) -> bool:
        """tail 保留額是否大到裝得下一整個「headroom 大小」的回合。

        False 代表:競態時如果那則使用者訊息比保留額大,它會被切進摘要
        (tail 從回合中段開始)。壓縮後的事後核對會抓到並要求重送 —— 這是
        可見的錯誤,不是靜默失真,但呼叫端應該把它講出來。
        """
        return self.preserve_recent_tokens >= self.headroom

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__slots__}


def _as_int(value: Any, label: str) -> int:
    """JSON 的數字只有一種:``131072.0`` 與 ``131072`` 是同一個值。

    只認 Python 的 `int` 的話,設定檔裡寫成 `.0` 的限制會讓推導在一端成功、
    在另一端報「推導失敗」—— 對同一份設定給出相反的答案。
    """
    if isinstance(value, bool):
        raise CompactionModeError(f"{label} 必須是數字,得到 {value!r}")
    if isinstance(value, float):
        if not value.is_integer():
            raise CompactionModeError(f"{label} 必須是整數,得到 {value!r}")
        return int(value)
    if not isinstance(value, int):
        raise CompactionModeError(f"{label} 必須是數字,得到 {value!r}")
    return value


def _positive_int(value: Any, label: str) -> int:
    number = _as_int(value, label)
    if number <= 0:
        raise CompactionModeError(f"{label} 必須是正整數,得到 {value!r}")
    return number


def effective_max_output(output_limit: Any) -> int:
    """有效輸出額:``min(limit.output, 32000) || 32000``。

    ``|| OUTPUT_TOKEN_MAX`` 那一段是真的:``limit.output`` 是 0 時退回 32000,
    不是 0。照這條算才不會在那種模型上得出完全不同的門檻。
    """
    number = _as_int(output_limit, "limit.output")
    if number < 0:
        raise CompactionModeError(f"limit.output 不得為負,得到 {output_limit!r}")
    capped = min(number, OUTPUT_TOKEN_MAX)
    return capped or OUTPUT_TOKEN_MAX


def derive_settings(
    *,
    context_limit: Any,
    output_limit: Any,
    input_limit: Any = None,
    reserved: Any = None,
) -> DerivedSettings:
    """依 2026-09 定案的公式 + CodeTrail 工具結果預算推導門檻(來源見 git 歷史)。

    可用輸入額::

        maxOutput = min(limit.output, 32000) || 32000
        reserved  = compaction.reserved ?? min(20000, maxOutput)
        usable    = limit.input ? max(0, limit.input - reserved)
                                : max(0, context - maxOutput)

    CodeTrail 在這上面再扣一份 headroom,提早在 idle 觸發::

        tool_budget = floor(context * 0.12)   # 單次工具結果預算
        headroom    = tool_budget + maxOutput # 一次完整工具結果 + 一次輸出
        threshold   = usable - headroom
        tail_cap    = threshold - maxOutput   # 壓縮後 summary + tail 仍要低於門檻
        preserve    = min(headroom, tail_cap)

    為什麼 headroom 還要再扣一次 maxOutput:``usable`` 扣掉的那一份是「這個
    請求的回覆」;工具迴圈裡助理**上一步**的輸出會變成下一步的輸入,所以那是
    第二份、不是同一份。壓縮只在 idle 觸發(沒有 mid-turn 壓縮、沒有
    provider-overflow 回復),所以那一輪只要越過 usable 就是可見的硬錯誤,
    沒有補救。保留「一次工具結果 + 一次輸出」是這個取捨下能推導出來的最小
    保證:再長的工具輪(6-8 次呼叫)仍會硬錯誤,那是計畫明確接受的限制。

    ``tail_cap`` 的意義:壓縮完之後 context 大約是「摘要 + tail」,而摘要最多
    就是模型的 ``maxOutput``。tail 若大到讓兩者加起來又越過門檻,下一次 idle
    會立刻再壓一次。這個上限完全由模型限制推導,沒有任何固定百分比。
    """
    context = _positive_int(context_limit, "limit.context")
    max_output = effective_max_output(output_limit)
    if max_output >= context:
        raise CompactionModeError(
            f"limit.output({max_output})不得 >= limit.context({context})"
        )
    if reserved is None:
        resolved_reserved = min(COMPACTION_RESERVE_TOKENS, max_output)
    else:
        # 公式用的是 `??`,不是 `||`:明確寫 0 的 `compaction.reserved` 保留 0。
        # 這裡拒絕 0 的話,對那種設定算出來的 usable 會少一整份 reserve。
        resolved_reserved = _as_int(reserved, "compaction.reserved")
        if resolved_reserved < 0:
            raise CompactionModeError(
                f"compaction.reserved 必須是非負整數,得到 {reserved!r}"
            )
    if not input_limit:
        # 公式是 `limit.input ? ... : ...`(truthiness),所以 0 與缺席一樣走
        # context 分支。這裡拒絕 0 的話,runtime 與設定端會得到相反的門檻。
        if input_limit is not None:
            _as_int(input_limit, "limit.input")
        usable = max(0, context - max_output)
    else:
        usable = max(0, _positive_int(input_limit, "limit.input") - resolved_reserved)

    tool_budget = max(1, math.floor(context * TOOL_RESULT_CONTEXT_FRACTION))
    headroom = tool_budget + max_output
    threshold = usable - headroom
    tail_cap = threshold - max_output
    if threshold < MIN_PRESERVE_RECENT_TOKENS or tail_cap < MIN_PRESERVE_RECENT_TOKENS:
        raise CompactionModeError(
            "模型的有效輸入容量太小,推導不出可用的壓縮門檻"
            f"(usable={usable}, headroom={headroom}, threshold={threshold},"
            f" tail_cap={tail_cap});請提高 limit.context 或改用 native 模式"
        )
    preserve = min(headroom, tail_cap)
    return DerivedSettings(
        context_limit=context,
        output_limit=max_output,
        input_limit=None if input_limit is None else int(input_limit),
        reserved=resolved_reserved,
        usable=usable,
        tool_result_budget=tool_budget,
        headroom=headroom,
        tail_cap=tail_cap,
        preserve_recent_tokens=preserve,
        idle_threshold=threshold,
    )
