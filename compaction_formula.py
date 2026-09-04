#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""compaction_formula — 壓縮門檻的推導公式與 canonical 規則文字。

這是 runtime 的那一半:客戶端(`client_compaction`)、`config` 的
``CLIENT_MAX_OUTPUT_TOKENS`` 上限檢查、`set_config` 的摘要頁與
`scripts/session_eval.py` 的指紋都吃這裡。

**這個模組不知道 OpenCode 的存在**,也不得 import `opencode_migrate`:
ownership 狀態檔、`opencode.json` 的受管鍵、plugin 項與 native 還原全部
住在那邊(它是使用者手動執行的一次性升級工具)。分開的理由是責任不同 ——
公式是每一輪都要算的東西,ownership 是一台機器一輩子跑一次的遷移。

**數值不是拍腦袋的百分比**:門檻與 tail 保留額由上游 `overflow.ts` /
`compaction.ts` 的公式,加上 CodeTrail 自己的單次工具結果預算推導出來
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
#: 上游 `session/overflow.ts` 的 COMPACTION_BUFFER。
UPSTREAM_COMPACTION_BUFFER = 20_000
#: 上游 `provider/transform.ts` 的 OUTPUT_TOKEN_MAX。有效輸出額是
#: ``min(limit.output, 32000) || 32000`` —— 不套這個 cap 的話,對
#: ``limit.output`` 很大的模型會算出比上游更早的門檻。
UPSTREAM_OUTPUT_TOKEN_MAX = 32_000
#: 上游 `session/compaction.ts` 的 MIN_PRESERVE_RECENT_TOKENS。
UPSTREAM_MIN_PRESERVE_RECENT_TOKENS = 2_000
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

#: 支援本次驗證過的 compaction 語意的最低 OpenCode 版本。
#: 1.18.15 起摘要請求的歷史從 model messages 改成序列化文字,1.18.17 起
#: `DEFAULT_TAIL_TURNS` 移除、`MAX_PRESERVE_RECENT_TOKENS` 由 8k 改 15k。

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

    找不到就 raise —— plugin 的規則字面值與文件不一致是靜默的失真來源
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
    plugin 端(`parseRuleHeadings`)對 `RULES_TEXT` 做同樣的解析,由跨語言測試
    釘住兩邊解出來的東西相同。
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
    (壓完留多少)。把這些值翻成別人設定檔的鍵名不是這裡的事:那份形狀住在
    `opencode_migrate.managed_values()`,而它是使用者手動執行的升級工具。
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

        False 代表:競態時如果那則使用者訊息比保留額大,上游會把它切進摘要
        (`splitTurn` 從回合中段建立 tail)。plugin 的事後核對會抓到並要求
        重送 —— 這是可見的錯誤,不是靜默失真,但呼叫端應該把它講出來。
        """
        return self.preserve_recent_tokens >= self.headroom

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__slots__}


def _as_int(value: Any, label: str) -> int:
    """JSON 的數字只有一種:``131072.0`` 與 ``131072`` 是同一個值。

    只認 Python 的 `int` 的話,設定裡寫成 `.0` 的限制在 JS 端(`Number.isInteger`
    接受)可以正常推導,Python 端(doctor / eval preflight)卻報「推導失敗」——
    兩邊對同一份設定給出相反的答案。
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
    """上游 `ProviderTransform.maxOutputTokens()`:``min(limit.output, 32000) || 32000``。

    ``|| outputTokenMax`` 那一段是真的:``limit.output`` 是 0 時上游退回 32000,
    不是 0。照抄才不會在那種模型上算出完全不同的門檻。
    """
    number = _as_int(output_limit, "limit.output")
    if number < 0:
        raise CompactionModeError(f"limit.output 不得為負,得到 {output_limit!r}")
    capped = min(number, UPSTREAM_OUTPUT_TOKEN_MAX)
    return capped or UPSTREAM_OUTPUT_TOKEN_MAX


def derive_settings(
    *,
    context_limit: Any,
    output_limit: Any,
    input_limit: Any = None,
    reserved: Any = None,
) -> DerivedSettings:
    """依上游公式 + CodeTrail 工具結果預算推導受管值。

    上游(`session/overflow.ts` usable / `session/compaction.ts`
    preserveRecentBudget)::

        maxOutput = min(limit.output, 32000) || 32000   # transform.ts
        reserved  = compaction.reserved ?? min(20000, maxOutput)
        usable    = limit.input ? max(0, limit.input - reserved)
                                : max(0, context - maxOutput)

    CodeTrail 在這上面再扣一份 headroom,提早在 idle 觸發::

        tool_budget = floor(context * 0.12)   # 單次工具結果預算
        headroom    = tool_budget + maxOutput # 一次完整工具結果 + 一次輸出
        threshold   = usable - headroom
        tail_cap    = threshold - maxOutput   # 壓縮後 summary + tail 仍要低於門檻
        preserve    = min(headroom, tail_cap)

    為什麼 headroom 還要再扣一次 maxOutput:上游的 ``usable`` 扣掉的那一份是
    「這個請求的回覆」;工具迴圈裡助理**上一步**的輸出會變成下一步的輸入,
    所以那是第二份、不是同一份。CodeTrail 模式關掉 `compaction.auto`,連帶
    關掉 mid-turn 壓縮與 provider-overflow 回復(overflow.ts L28 一律回
    false),所以 idle 之後那一輪只要越過 usable 就是可見的硬錯誤,沒有補救。
    保留「一次工具結果 + 一次輸出」是這個取捨下能推導出來的最小保證:再長的
    工具輪(6-8 次呼叫)仍會硬錯誤,那是計畫明確接受的限制。

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
        resolved_reserved = min(UPSTREAM_COMPACTION_BUFFER, max_output)
    else:
        # 上游用 `??`,不是 `||`:明確寫 0 的 `compaction.reserved` 保留 0。
        # 這裡拒絕 0 的話,對那種設定算出來的 usable 會比上游少一整份 reserve。
        resolved_reserved = _as_int(reserved, "compaction.reserved")
        if resolved_reserved < 0:
            raise CompactionModeError(
                f"compaction.reserved 必須是非負整數,得到 {reserved!r}"
            )
    if not input_limit:
        # 上游是 `limit.input ? ... : ...`(truthiness),所以 0 與缺席一樣走
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
    if threshold < UPSTREAM_MIN_PRESERVE_RECENT_TOKENS or tail_cap < UPSTREAM_MIN_PRESERVE_RECENT_TOKENS:
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


def combine_settings(summariser: DerivedSettings, live: DerivedSettings) -> DerivedSettings:
    """`agent.compaction.model` 指到另一個模型時,兩個模型都要活下來。

    上游用 compaction agent 的模型(``summariser``)做摘要與 tail selection,但
    **觸發之前**那整段對話壓的是主模型(``live``),壓縮之後留下來的摘要與 tail
    也還要繼續進主模型。只按摘要模型推導的話,摘要模型 context 較大時門檻會高
    過主模型裝得下的量:主模型先 overflow,而 `compaction.auto=false` 已經把上游
    的自動回復關掉了,使用者看到的是 context error。

    合併之後的每一欄仍然滿足 `derive_settings` 的同一組關係式,不是逐欄取
    min/max 拼出來的:

      * ``output_limit`` 取兩者較大者 —— 落進 session 的單一完成有兩種可能
        (摘要模型產出的摘要、主模型產出的回答),要擋住比較大的那個。
        兩個模型的 `limit.output` 不同時,只看其中一邊會讓 ``tail_cap`` 算得
        比實際寬:例如主模型 32768/8192 配 131072/32000 的摘要模型,一份 25K
        的合法摘要就已經塞不回主模型。
      * ``tool_result_budget`` 取較小者、``usable`` 取較小者、``reserved`` 取
        較大者 —— 都是「兩邊都得成立」的方向。
      * ``headroom`` 與 ``tail_cap`` 由上面那幾個**重新推導**,所以
        ``headroom == tool_result_budget + output_limit`` 與
        ``tail_cap == idle_threshold - output_limit`` 仍然成立。
      * ``idle_threshold`` 取兩個模型各自的門檻與合併預算三者的較小值。

    兩個模型相同時逐欄與 `derive_settings` 的結果相同。合併之後推不出可用門檻
    (與單一模型同一條下界)就 raise —— 那個組合本來就不該被寫進設定。
    """
    output = max(summariser.output_limit, live.output_limit)
    tool_budget = min(summariser.tool_result_budget, live.tool_result_budget)
    usable = min(summariser.usable, live.usable)
    headroom = tool_budget + output
    threshold = min(summariser.idle_threshold, live.idle_threshold, usable - headroom)
    tail_cap = threshold - output
    if threshold < UPSTREAM_MIN_PRESERVE_RECENT_TOKENS or tail_cap < UPSTREAM_MIN_PRESERVE_RECENT_TOKENS:
        raise CompactionModeError(
            "摘要模型與主模型合起來推導不出可用的壓縮門檻"
            f"(usable={usable}, headroom={headroom}, threshold={threshold},"
            f" tail_cap={tail_cap});請讓兩個模型的 limit 更接近,或改用 native 模式"
        )
    inputs = [
        value for value in (summariser.input_limit, live.input_limit) if value is not None
    ]
    return DerivedSettings(
        context_limit=min(summariser.context_limit, live.context_limit),
        output_limit=output,
        input_limit=min(inputs) if inputs else None,
        reserved=max(summariser.reserved, live.reserved),
        usable=usable,
        tool_result_budget=tool_budget,
        headroom=headroom,
        tail_cap=tail_cap,
        preserve_recent_tokens=min(headroom, tail_cap),
        idle_threshold=threshold,
    )

