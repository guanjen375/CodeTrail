"""OpenCode 壓縮模式(compaction mode)的單一真值。

**這套接管仍在測試階段**(`codetrail` / `manual`;見 `EXPERIMENTAL_NOTICE`)。
`native` 不在其中 —— 那條路徑就是「不接管」,行為與這個功能出現之前一模一樣。

CodeTrail 對 OpenCode 的自動壓縮有三種模式,由 `./set_config.sh` 顯式選擇:

  * ``codetrail``  結構化壓縮(建議,但那一題沒有預設值):關掉上游「下一個
    prompt 進來才檢查」的
    自動路徑,改由 plugin 在助理答完、session idle 之後主動觸發,並用固定
    欄位的摘要規則。顯式選擇本身就是「授權接管」的那個動作,所以互動題
    刻意沒有預設值(Enter 不能過關)。
  * ``manual``     同一套摘要規則,但不主動觸發 —— 使用者自己按 ``/compact``。
  * ``native``     完全恢復使用者原本的 OpenCode 行為(精確還原接管前的值)。

**為什麼需要一份狀態檔**:``opencode.json`` 沒有任何欄位記得「這個值是
CodeTrail 寫的還是使用者寫的」。而模式切換必須覆寫既有鍵(``compaction.auto``
要在 true / false 之間翻),這跟 repo 其他 writer 的「只補缺、使用者設過的一律
尊重」原則相反。所以接管前把每個受管鍵的原值(或「原本不存在」)記進
owner-only 的狀態檔;切回 native 時只還原**仍帶 ownership 證據**的鍵 ——
使用者事後手改過的值,CodeTrail 不再認為自己擁有,還原時原封不動。
上一份狀態是 ``native``(代表我們什麼都不擁有了)時,baseline 會以**現況**
重記:使用者在 native 期間自己加回來的東西是他的,不能被第一次接管前的舊
事實刪掉。

**沒有狀態檔 = 沒有接管**。舊安裝 ``git pull`` 之後不會突然多一個壓縮 plugin:
`load_state()` 回 None 時,contract check 不補 plugin、plugin 自己也不動作。
這條是 fail-closed 的,不是「預設 codetrail」。`DEFAULT_MODE` 只是**建議**
的那一個 —— set_config 的那一題沒有預設值,Enter 不能過關(顯式選擇本身就是
「授權接管」的那個動作)。

**狀態檔綁一份 config**。`config` 欄位記的是目標 opencode.json 的身分雜湊
(路徑與 realpath 各一份,**兩個都要相符**);`digest` 記的是整份 ownership
紀錄,包含 `prior`。兩者在還原前都要核對:不核對的話,拿 A 設定的狀態去切
B 設定的 native,會把 A 的原值寫進 B;而被人手改過的 `prior` 會原樣被信任。

**數值不是拍腦袋的百分比**:門檻與 tail 保留額由上游 `overflow.ts` /
`compaction.ts` 的公式,加上 CodeTrail 自己的單次工具結果預算推導出來
(見 `derive_settings`)。JS plugin 端有逐字相同的一份,由
`tests/test_opencode_compaction_plugin.py` 的跨語言測試釘住:兩邊只要有一邊
改了公式,plugin 的觸發門檻就會跟寫進 opencode.json 的保留額對不起來,而
兩邊各自的測試都是綠的。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parent

# ---- 模式 ----------------------------------------------------------------
MODE_CODETRAIL = "codetrail"
MODE_NATIVE = "native"
MODE_MANUAL = "manual"
COMPACTION_MODES = (MODE_CODETRAIL, MODE_NATIVE, MODE_MANUAL)
#: set_config 問答的預設選項。**不是**「沒有狀態檔時的行為」(那是 native)。
DEFAULT_MODE = MODE_CODETRAIL
#: 需要載入 CodeTrail 壓縮 plugin 的模式。
PLUGIN_MODES = (MODE_CODETRAIL, MODE_MANUAL)
#: plugin 會主動在 idle 觸發壓縮的模式。
AUTO_TRIGGER_MODES = (MODE_CODETRAIL,)

MODE_LABELS = {
    MODE_CODETRAIL: "CodeTrail 結構化壓縮(答完進 idle 才壓縮)",
    MODE_NATIVE: "OpenCode 原生壓縮(還原你原本的設定)",
    MODE_MANUAL: "手動結構化壓縮(同一套規則,只在 /compact 時執行)",
}

# ---- 開發階段標示 --------------------------------------------------------
#: CodeTrail 接管壓縮(codetrail / manual)還在測試階段。`native` 不標 ——
#: 那條路徑就是「不接管」,沒有實驗成分。
#:
#: 為什麼要有這兩個常數:同一句話要出現在 set_config 的問答、set_config 的
#: 設定摘要、aicode 啟動橫幅、doctor 與使用者文件。五個地方各寫各的,拿掉
#: 其中一個就會有人在完全不知道的情況下把長對話交給一個還在調整的機制。
EXPERIMENTAL_MODES = PLUGIN_MODES
#: 短標籤:接在模式名後面(選項列、狀態行、摘要頁)。
EXPERIMENTAL_TAG = "🧪 實驗中"
#: 完整說明:自成一行的警告(問答、啟動橫幅、文件標題下)。**不含**行動建議 ——
#: 每個呼叫端接的下一句不一樣(問答說「選 native」、橫幅說那行命令),寫進來
#: 就會有一半的地方讀起來像廢話。
EXPERIMENTAL_NOTICE = "🧪 實驗功能(開發中、仍在測試階段):行為與受管值可能再變"


def is_experimental(mode: str | None) -> bool:
    """這個模式還在測試階段嗎?(`native` = 原本的行為,不是。)"""
    return mode in EXPERIMENTAL_MODES


def mode_tag(mode: str | None) -> str:
    """顯示用後綴:實驗模式回 `" 🧪 實驗中"`,其餘回空字串。"""
    return f" {EXPERIMENTAL_TAG}" if is_experimental(mode) else ""

# ---- plugin 與規則 --------------------------------------------------------
PLUGIN_FILENAME = "codetrail-compaction.js"
PLUGIN_PATH = REPO_ROOT / "opencode_plugins" / PLUGIN_FILENAME
#: 七條摘要規則的 canonical 文件。plugin 內的規則字面值必須與這裡的
#: ```text 區塊逐字一致(跨檔一致性由 tests 釘住)。
RULES_DOC = REPO_ROOT / "docs" / "compaction-rules.md"

# ---- 狀態檔 --------------------------------------------------------------
COMPACTION_STATE_SCHEMA = 1
STATE_DIR_PARTS = (".config", "codetrail")
STATE_FILENAME = "compaction.json"
STATE_FILE_MODE = 0o600
STATE_DIR_MODE = 0o700

# ---- 受管欄位 ------------------------------------------------------------
#: opencode.json 的 ``compaction`` 物件裡由 CodeTrail 擁有的鍵。
MANAGED_COMPACTION_KEYS = ("auto", "tail_turns", "preserve_recent_tokens")
COMPACTION_SECTION = "compaction"
PLUGIN_SECTION = "plugin"

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
#: 同一份 plugin 規則在 1.18.16 以前拿到的是不同語意 —— 那正是「靜默退回
#: 不同語意」,所以壓縮功能自己再設一道版本閘,不沿用 direct-contract 的 1.17.0。
MIN_COMPACTION_OPENCODE_VERSION = (1, 18, 17)


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
# JSON 型別嚴格比較
# ---------------------------------------------------------------------------
def json_equal(left: Any, right: Any) -> bool:
    """JSON 語意的相等。

    兩條規則,兩條都是為了跟 JS 端講同一種話:

    * ``True`` 與 ``1`` **不同**。Python 的 ``True == 1`` 會讓 ownership 判斷把
      使用者手改成 boolean 的 ``tail_turns`` 當成「還是我們寫的 1」,於是切回
      native 時把他的值蓋掉。
    * ``1`` 與 ``1.0`` **相同**。JSON 只有一種數字型別,``JSON.parse("1.0")``
      在 JS 就是 ``1`` —— 把它們判成不同,plugin 會說「沒漂移」而 Python 說
      「漂移了」,兩邊對同一份設定給出相反的答案。
    """
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(
            json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            json_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, (dict, list)) or isinstance(right, (dict, list)):
        return False
    return type(left) is type(right) and left == right


# ---------------------------------------------------------------------------
# 推導
# ---------------------------------------------------------------------------
class DerivedSettings:
    """一個模型在 CodeTrail / manual 模式下的受管值與 idle 門檻。

    ``config_values`` 是要寫進 ``opencode.json`` 的部分;``idle_threshold``
    不是 OpenCode 的 schema 鍵,由 plugin 自己用同一條公式在 runtime 算。
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
    def config_values(self) -> dict[str, Any]:
        return {
            "auto": False,
            "tail_turns": TAIL_TURNS,
            "preserve_recent_tokens": self.preserve_recent_tokens,
        }

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


# ---------------------------------------------------------------------------
# 狀態檔
# ---------------------------------------------------------------------------
def compaction_model_limits(config: dict[str, Any], fallback: str | None = None):
    """`agent.compaction.model` 指定的模型限制;沒設就用 ``fallback``。

    上游做 tail selection 與摘要用的是 **compaction agent 的模型**。writer 用
    主模型推導、runtime 用 compaction agent 重算,兩邊算出來的受管值不同時,
    設定寫完的第一個 idle 就會被判成 `config_drift` —— 而使用者什麼都沒做錯。

    回 ``(model_ref, limit_dict, configured)``。``configured`` 表示
    ``agent.compaction.model`` 有沒有被明確設定 —— 有設但查不到 limit 時呼叫端
    必須 fail-loud,不能靜默退回主模型的公式(那會寫出一開機就被判 drift 的值)。
    """
    agent = config.get("agent")
    entry = agent.get("compaction") if isinstance(agent, dict) else None
    configured = entry.get("model") if isinstance(entry, dict) else None
    explicit = isinstance(configured, str) and "/" in configured
    model = configured if explicit else fallback
    if not isinstance(model, str) or "/" not in model:
        return None, None, explicit
    return model, model_limit(config, model), explicit


def model_limit(config: dict[str, Any], ref: Any) -> dict[str, Any] | None:
    """``provider.<id>.models.<id>.limit``;查不到就 None。"""
    if not isinstance(ref, str) or "/" not in ref:
        return None
    provider_id, model_id = ref.split("/", 1)
    providers = config.get("provider")
    provider = providers.get(provider_id) if isinstance(providers, dict) else None
    models = provider.get("models") if isinstance(provider, dict) else None
    spec = models.get(model_id) if isinstance(models, dict) else None
    limit = spec.get("limit") if isinstance(spec, dict) else None
    return limit if isinstance(limit, dict) else None


def derive_for_config(
    config: dict[str, Any],
) -> tuple[DerivedSettings, str] | None:
    """依這份**有效設定**的模型限制推導受管值,回 ``(受管值, 顯示用來源)``。

    回 ``None`` 代表這份設定沒有足以推導的模型資訊 —— 是「無從得知」,不是錯。
    該有的 limit 查不到、或推不出可用門檻時 raise ``CompactionModeError``,
    訊息本身就是要給人看的那一句。

    為什麼要收在這裡:doctor 的漂移比對與 `aicode` 橫幅顯示的門檻必須是同一個
    數字,而 runtime 的 plugin 也用同一條公式重算。各寫一份的話,doctor 說一致、
    橫幅印另一個數字,而使用者看到的停用理由來自第三個。
    """
    main_model = config.get("model")
    model, limit, explicit = compaction_model_limits(config, fallback=main_model)
    if model is None:
        return None
    section, _ = _section(config)
    reserved = section.get("reserved") if isinstance(section, dict) else None
    if limit is None:
        if explicit:
            # 明確設了 compaction agent 的模型卻查不到 limit:runtime 會用
            # `client.config.providers()` 拿到它的真實 limit 重算並可能停用,
            # 這裡靜靜跳過就會回報「一致」而 plugin 那端已經停了。
            raise CompactionModeError(
                f"agent.compaction.model 設成 {model},但設定裡沒有它的 limit;"
                "無法確認受管值是否與 runtime 一致(plugin 會用真實 limit 重算)"
            )
        return None
    try:
        derived = derive_settings(
            context_limit=limit.get("context"),
            output_limit=limit.get("output"),
            input_limit=limit.get("input"),
            reserved=reserved,
        )
    except CompactionModeError as exc:
        raise CompactionModeError(
            f"目前的 {model} 推導不出可用的壓縮門檻({exc});"
            "壓縮 plugin 會停用自動壓縮,請重跑 ./set_config.sh"
        ) from exc
    if not (explicit and main_model != model):
        return derived, f"{model}(context={limit.get('context')})"

    # 摘要模型與主模型不同時,受管值是兩者的合併值:觸發之前那段對話壓的是
    # 主模型,只按摘要模型算的話,摘要模型 context 較大時主模型會先 overflow。
    main_limit = model_limit(config, main_model)
    if main_limit is None:
        raise CompactionModeError(
            f"agent.compaction.model 設成 {model},但設定裡沒有主模型 "
            f"{main_model} 的 limit;無法確認受管值是否與 runtime 一致"
            "(plugin 會用兩個模型的真實 limit 合併重算)"
        )
    try:
        derived = combine_settings(
            derived,
            derive_settings(
                context_limit=main_limit.get("context"),
                output_limit=main_limit.get("output"),
                input_limit=main_limit.get("input"),
                reserved=reserved,
            ),
        )
    except CompactionModeError as exc:
        raise CompactionModeError(
            f"主模型 {main_model} 推導不出可用的壓縮門檻({exc});"
            "壓縮 plugin 會停用自動壓縮,請重跑 ./set_config.sh"
        ) from exc
    return derived, f"{model} 與主模型 {main_model} 取合併值"


def state_dir(env: Any = None) -> Path:
    values = os.environ if env is None else env
    raw = (values.get("HOME") or values.get("USERPROFILE") or "").strip()
    if not raw:
        raise CompactionModeError("找不到 HOME,無法定位 CodeTrail 狀態目錄")
    return Path(raw).expanduser().joinpath(*STATE_DIR_PARTS)


def state_path(env: Any = None) -> Path:
    return state_dir(env) / STATE_FILENAME


def config_identity(path: Path) -> dict[str, str]:
    """記住「這份狀態描述的是哪一個 opencode.json」。

    非預設 ``OPENCODE_CONFIG`` 很常見(eval、多份設定),路徑與 realpath 各記
    一份雜湊:換了目標檔就不能拿舊狀態去還原別人的設定。不存明文路徑 ——
    狀態檔本身是零內容契約的一部分。

    **兩個雜湊都要相符**(見 `state_matches_config`)。相對路徑的
    ``OPENCODE_CONFIG`` 會依**呼叫端的 cwd** 展開,兩端 cwd 不同時比對會不符
    —— 那個方向是 fail-closed(當成沒有接管),不會誤動別人的設定。
    """
    text = os.path.abspath(str(path))
    try:
        real = str(Path(path).resolve(strict=False))
    except OSError:
        real = text
    return {
        "path_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:32],
        "real_path_hash": hashlib.sha256(real.encode("utf-8")).hexdigest()[:32],
    }


def state_matches_config(state: dict[str, Any] | None, config_path: Path) -> bool:
    """狀態檔描述的是不是**這一份** config。

    **兩個雜湊都要相符**。只比對其中一個都有洞:只看 ``path_hash`` 的話,
    ``OPENCODE_CONFIG`` 指著的那個 symlink 被改指到另一份設定時,路徑沒變、
    比對照樣通過,於是 A 的「接管前原值」會被寫進 B;只看 ``real_path_hash``
    的話,兩份不同的 symlink 指到同一個目標會被當成同一份。
    """
    if not state:
        return False
    recorded = state.get("config")
    if not isinstance(recorded, dict):
        return False
    current = config_identity(config_path)
    return all(
        recorded.get(key) == current[key] for key in ("path_hash", "real_path_hash")
    )


def _prior_record(present: bool, value: Any) -> dict[str, Any]:
    return {"present": True, "value": value} if present else {"present": False}


#: JS 的 Number 是 IEEE-754 double;超過這個範圍的整數在它那邊會被捨入,
#: 兩端就永遠算不出同一個 digest。
JS_SAFE_INTEGER = 2**53 - 1


def _digest_safe(value: Any, path: str) -> None:
    """確認這個值在 Python 與 JS 兩端會序列化成**同一段文字**。

    不檢查的話,`prior` 裡的值就可能讓 plugin 把 set_config 寫出的**正確**狀態
    算成 digest 不符,然後靜默當成沒有接管。已知會分歧的三類:

      * `1e-7` —— Python 寫 `1e-07`、JS 寫 `1e-7`。
      * 超過 2^53 的整數 —— JS 的 Number 會捨入。
      * 物件的鍵順序與非 ASCII 字串 —— Python 的 `sort_keys` 按 code point、
        JS 的 `.sort()` 按 UTF-16 code unit;lone surrogate 的跳脫方式也不同。

    所以只接受**純量**,而且字串限 ASCII。這三個受管鍵(`auto` / `tail_turns`
    / `preserve_recent_tokens`)在上游 schema 裡本來就是 boolean / number,
    容器或非 ASCII 字串都不是有意義的值,擋掉不構成實務限制。
    """
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if not value.isascii():
            raise CompactionModeError(
                f"{path} 的值是非 ASCII 字串,兩端的 JSON 跳脫方式可能不同;"
                "請先把它改成布林或整數再設定壓縮模式"
            )
        return
    if isinstance(value, int):
        if abs(value) > JS_SAFE_INTEGER:
            raise CompactionModeError(
                f"{path} 的值 {value} 超過 JavaScript 的安全整數範圍,"
                "兩端算不出同一個 digest;請先把它改成較小的整數再設定壓縮模式"
            )
        return
    if isinstance(value, float):
        if not value.is_integer() or abs(value) > JS_SAFE_INTEGER:
            raise CompactionModeError(
                f"{path} 的值 {value!r} 不是可安全跨語言序列化的數字;"
                "請先把它改成整數或布林再設定壓縮模式"
            )
        return
    raise CompactionModeError(
        f"{path} 的值型別無法寫進壓縮狀態({type(value).__name__});"
        "這三個鍵在 OpenCode schema 裡是 boolean / number,請先改成那兩種之一"
    )


def _digest_normalise(value: Any) -> Any:
    """把值正規化成「JS 讀完這份 JSON 之後會拿到的東西」。

    JS 端要能算出同一個 digest,但 ``JSON.parse`` 已經把 ``1.0`` 變成 ``1``,
    那個資訊在它那邊救不回來。所以由 Python 這端讓步:整數值的 float 一律
    當整數雜湊。不做的話,使用者原本合法寫成 ``"tail_turns": 1.0`` 的設定會
    讓 plugin 把 set_config 寫出的**正確**狀態判成被竄改,然後靜默停用。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _digest_normalise(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_digest_normalise(item) for item in value]
    return value


def digest_managed(values: dict[str, Any]) -> str:
    payload = json.dumps(
        _digest_normalise(values), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _state_digest(state: dict[str, Any]) -> str:
    """整份 ownership 紀錄的 digest(不只是我們寫下去的值)。

    只雜湊「寫下去的值」的話,把 `prior` 改掉不會讓 digest 失效 —— 而 `prior`
    正是還原時會被寫回 config 的東西。
    """
    managed = state.get("managed", {})
    payload = {
        "mode": state.get("mode"),
        "config": state.get("config"),
        "plugin": state.get("plugin"),
        "section_present": state.get("section_present"),
        "managed": {
            key: {"value": entry.get("value"), "prior": entry.get("prior")}
            for key, entry in managed.items()
        },
    }
    return digest_managed(payload)


def build_state(
    *,
    mode: str,
    config_path: Path,
    managed: dict[str, dict[str, Any]],
    plugin: dict[str, Any],
    section_present: bool,
) -> dict[str, Any]:
    if mode not in COMPACTION_MODES:
        raise CompactionModeError(f"未知的壓縮模式:{mode!r}")
    for key, entry in managed.items():
        prior = entry.get("prior")
        if isinstance(prior, dict) and prior.get("present"):
            _digest_safe(prior.get("value"), f"{COMPACTION_SECTION}.{key}")
    state = {
        "schema": COMPACTION_STATE_SCHEMA,
        "mode": mode,
        "config": config_identity(config_path),
        "managed": managed,
        "plugin": plugin,
        # 接管前 `compaction` 區塊本身存不存在。少了這一格,還原時就分不出
        # 「區塊是我們建的」與「使用者本來就有一個空物件」,後者會被順手刪掉。
        "section_present": section_present,
    }
    state["digest"] = _state_digest(state)
    return state


def validate_state(value: Any) -> dict[str, Any] | None:
    """形狀不對或 digest 對不上就當作沒有狀態(fail-closed)。"""
    if not isinstance(value, dict):
        return None
    if value.get("schema") != COMPACTION_STATE_SCHEMA:
        return None
    if value.get("mode") not in COMPACTION_MODES:
        return None
    managed = value.get("managed")
    if not isinstance(managed, dict):
        return None
    for key, entry in managed.items():
        if key not in MANAGED_COMPACTION_KEYS:
            return None
        if not isinstance(entry, dict) or "value" not in entry:
            return None
        prior = entry.get("prior")
        if not isinstance(prior, dict) or not isinstance(prior.get("present"), bool):
            return None
        if prior["present"] and "value" not in prior:
            return None
    if not isinstance(value.get("plugin"), dict):
        return None
    if not isinstance(value.get("config"), dict):
        return None
    if not isinstance(value.get("section_present"), bool):
        return None
    if not isinstance(value.get("digest"), str):
        return None
    if value["digest"] != _state_digest(value):
        return None
    return value


def _open_state_dir(
    parent: Path, *, create: bool, enforce_mode: bool = True
) -> int:
    """以 O_NOFOLLOW 開啟狀態目錄,並驗證擁有者與權限,回傳 dir fd。

    只有這一條路徑能拿到後續讀寫用的 fd:``~/.config/codetrail`` 本身被換成
    symlink 時,對其下檔案做 ``is_symlink()`` 仍然是 False,程式會乖乖跟過去
    覆寫別人目錄裡的檔案。

    權限一律收斂／要求 ``STATE_DIR_MODE``(0700)。舊安裝那個 umask 022 建
    出來的 0755 目錄在寫入路徑會被就地收緊(set_config 會把這件事印出來),
    讀取路徑則直接拒絕 —— 半套的判準(只擋 group/other write)會讓
    AGENTS.md §2 宣告的 0700 契約與實際行為對不上。
    """
    if create:
        try:
            parent.mkdir(parents=True, mode=STATE_DIR_MODE, exist_ok=True)
        except OSError as exc:
            raise CompactionModeError(f"無法建立狀態目錄 {parent}:{exc}") from exc
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(parent, flags)
    except OSError as exc:
        raise CompactionModeError(
            f"無法安全開啟狀態目錄 {parent}(是 symlink 或不是目錄?):{exc}"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise CompactionModeError(f"{parent} 不是目錄")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise CompactionModeError(f"狀態目錄 {parent} 不屬於目前使用者")
        mode = stat.S_IMODE(info.st_mode)
        if mode != STATE_DIR_MODE and create:
            # 寫入路徑順手收緊:目錄多半是舊版 set_config 用 umask 022/002
            # 建出來的,而這裡面放的是「切回 native 要寫回去什麼」。
            os.fchmod(fd, STATE_DIR_MODE)
        elif mode & 0o077 and enforce_mode:
            raise CompactionModeError(
                f"狀態目錄 {parent} 對其他帳號開放(mode {mode:o});"
                f"先 chmod {STATE_DIR_MODE:o} 再重試"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def check_state_dir(parent: Path | None = None, env: Any = None) -> str | None:
    """**不建立、不修改**任何東西地檢查狀態目錄,回不安全的理由或 None。

    給「用別的 writer 寫狀態檔」的呼叫端(set_config 走自己的 transaction,
    而且在使用者於摘要頁確認之前不得建立任何目錄)當前置檢查。

    只看修不好的條件:symlink、不是目錄、不屬於自己。權限鬆不算不安全 ——
    真正的寫入(set_config 的 transaction 或 `save_state()`)會在寫之前把它
    收斂成 0700。目錄還不存在也是正常的。
    """
    target = (state_dir(env) if parent is None else Path(parent))
    if not target.exists() and not target.is_symlink():
        return None
    try:
        fd = _open_state_dir(target, create=False, enforce_mode=False)
    except CompactionModeError as exc:
        return str(exc)
    os.close(fd)
    return None


def inspect_state(
    path: Path | None = None, env: Any = None
) -> tuple[dict[str, Any] | None, str | None]:
    """讀狀態檔,回 ``(state, reason)``。

    ``state`` 為 None 時 ``reason`` 說明為什麼(給 doctor / set_config 顯示);
    ``reason`` 為 None 且 state 為 None 代表「本來就沒有這個檔」= 沒有接管。
    """
    try:
        target = state_path(env) if path is None else Path(path)
    except CompactionModeError as exc:
        return None, str(exc)
    if not target.parent.exists() and not target.parent.is_symlink():
        return None, None
    # 先用不看權限的方式確認「這個檔到底存不存在」。舊安裝的
    # ~/.config/codetrail 常常是 umask 022 建出來的 0755,而裡面根本沒有
    # compaction.json —— 那是「沒有接管」,不該報成安全警告。
    try:
        probe_fd = _open_state_dir(target.parent, create=False, enforce_mode=False)
    except CompactionModeError as exc:
        return None, str(exc)
    try:
        exists = os.path.lexists(os.path.join(str(target.parent), target.name))
    finally:
        os.close(probe_fd)
    if not exists:
        return None, None
    try:
        dir_fd = _open_state_dir(target.parent, create=False)
    except CompactionModeError as exc:
        return None, str(exc)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_fd = os.open(target.name, flags, dir_fd=dir_fd)
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            return None, f"壓縮狀態檔無法安全開啟({target}):{exc}"
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                return None, f"壓縮狀態檔不是常規檔:{target}"
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                return None, f"壓縮狀態檔不屬於目前使用者:{target}"
            if stat.S_IMODE(info.st_mode) & 0o077:
                return None, (
                    f"壓縮狀態檔對其他帳號開放(mode {stat.S_IMODE(info.st_mode):o});"
                    "拒絕採信裡面的接管紀錄"
                )
            try:
                with os.fdopen(os.dup(file_fd), "r", encoding="utf-8") as handle:
                    raw = handle.read()
            except (OSError, UnicodeDecodeError) as exc:
                return None, f"壓縮狀態檔讀不出來({target}):{exc}"
        finally:
            os.close(file_fd)
    finally:
        os.close(dir_fd)

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None, f"壓縮狀態檔不是合法 JSON:{target}"
    state = validate_state(parsed)
    if state is None:
        return None, f"壓縮狀態檔的形狀或 digest 不符,已忽略:{target}"
    return state, None


def load_state(path: Path | None = None, env: Any = None) -> dict[str, Any] | None:
    """讀狀態檔。不存在 / 壞掉 / 權限不安全 → None(＝沒有接管)。"""
    try:
        state, _ = inspect_state(path, env)
    except CompactionModeError:
        return None
    except OSError:
        return None
    return state


def state_payload(state: dict[str, Any]) -> str:
    return json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def save_state(state: dict[str, Any], path: Path | None = None, env: Any = None) -> Path:
    """原子寫入 owner-only 狀態檔;拒絕 symlink 目錄與 symlink 目標。

    建立、chmod、rename 全部走同一個 dir fd:先用路徑檢查再用路徑寫入的話,
    中間那個空隙足夠把目錄或目標換成 symlink(TOCTOU)。
    """
    target = state_path(env) if path is None else Path(path)
    dir_fd = _open_state_dir(target.parent, create=True)
    temp_name: str | None = None
    try:
        try:
            existing = os.lstat(target.name, dir_fd=dir_fd)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise CompactionModeError(f"無法檢查 {target}:{exc}") from exc
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise CompactionModeError(
                f"壓縮狀態檔是 symlink({target}) —— 拒絕寫入,請先移除它"
            )
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
        )
        for _ in range(8):
            candidate = f".{target.name}.{os.urandom(6).hex()}.tmp"
            try:
                fd = os.open(candidate, flags, STATE_FILE_MODE, dir_fd=dir_fd)
            except FileExistsError:
                continue
            except OSError as exc:
                raise CompactionModeError(f"無法建立狀態暫存檔:{exc}") from exc
            temp_name = candidate
            break
        else:  # pragma: no cover - 8 次隨機名字全撞的機率可以忽略
            raise CompactionModeError("無法建立狀態暫存檔:名稱重複")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(state_payload(state))
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), STATE_FILE_MODE)
        os.replace(temp_name, target.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        temp_name = None
    except BaseException:
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=dir_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(dir_fd)
    return target


def clear_state(path: Path | None = None, env: Any = None) -> bool:
    try:
        target = state_path(env) if path is None else Path(path)
    except CompactionModeError:
        return False
    # 走與寫入同一條入口:父目錄被換成 symlink 時,只檢查最終檔案的話會刪掉
    # 連結目錄裡的別人檔案。
    try:
        dir_fd = _open_state_dir(target.parent, create=False, enforce_mode=False)
    except CompactionModeError:
        return False
    try:
        info = os.lstat(target.name, dir_fd=dir_fd)
        if stat.S_ISLNK(info.st_mode):
            raise CompactionModeError(
                f"壓縮狀態檔是 symlink({target}) —— 拒絕刪除,請先確認它指向哪裡"
            )
        os.unlink(target.name, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CompactionModeError(f"無法移除壓縮狀態檔:{exc}") from exc
    finally:
        os.close(dir_fd)
    return True


# ---------------------------------------------------------------------------
# 套用 / 還原
# ---------------------------------------------------------------------------
def _section(config: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """回 ``(section, error)``。

    「鍵不存在」與「鍵存在但值是 null」是兩件事:後者是使用者設過的值,
    當成不存在就會被我們順手刪掉。
    """
    if COMPACTION_SECTION not in config:
        return None, None
    section = config[COMPACTION_SECTION]
    if not isinstance(section, dict):
        return None, (
            f"{COMPACTION_SECTION} 必須是 JSON object,得到 "
            f"{'null' if section is None else type(section).__name__}"
        )
    return section, None


def owns(state: dict[str, Any] | None, key: str, config: dict[str, Any]) -> bool:
    """CodeTrail 是否仍握有這個鍵的 ownership 證據。

    證據 = 狀態檔記得自己寫了什麼,而**現在的值仍然是那個值**(JSON 型別
    嚴格相等)。使用者事後改過就不再是我們的 —— 還原時原封不動,不能拿舊的
    prior 蓋掉他的選擇。
    """
    if not state:
        return False
    entry = state.get("managed", {}).get(key)
    if not isinstance(entry, dict) or "value" not in entry:
        return False
    section, error = _section(config)
    if error is not None or not isinstance(section, dict) or key not in section:
        return False
    return json_equal(section[key], entry["value"])


def apply_mode(
    config: dict[str, Any],
    *,
    mode: str,
    derived: DerivedSettings | None,
    prior_state: dict[str, Any] | None,
    config_path: Path,
    plugin_path: Path | None = None,
) -> tuple[list[str], list[str], list[str], dict[str, Any] | None]:
    """把某個模式套進 config(in-place)。

    回傳 ``(changes, warnings, errors, state)``。``errors`` 非空時呼叫端不得
    寫檔;``state`` 為 None 代表這個模式不需要狀態檔(native 且已完成還原)。
    """
    if mode not in COMPACTION_MODES:
        return [], [], [f"未知的壓縮模式:{mode!r}"], None
    section, error = _section(config)
    if error is not None:
        return [], [], [error], None
    if prior_state is not None and not state_matches_config(prior_state, config_path):
        return [], [], [
            "壓縮狀態檔記錄的是另一份 opencode.json;拒絕拿它的接管紀錄動這一份。"
            "請先對原本那份切回 native,或移除 ~/.config/codetrail/compaction.json"
        ], None
    if mode == MODE_NATIVE:
        return _restore_native(
            config,
            prior_state=prior_state,
            plugin_path=plugin_path,
            config_path=config_path,
        )
    if derived is None:
        return [], [], ["缺少推導後的壓縮受管值"], None

    changes: list[str] = []
    warnings: list[str] = []
    # baseline 只在「我們現在還握著它」時才沿用。上一份狀態是 native 代表已經
    # 全部還原、我們什麼都不擁有了 —— 這時使用者在 native 期間新加的
    # `compaction` 區塊與 plugin 項是**他的**,要以現況重新記一次 baseline,
    # 不能拿第一次接管前的舊事實把它們刪掉。
    still_owned = bool(prior_state) and prior_state.get("mode") in PLUGIN_MODES
    if still_owned and isinstance(prior_state.get("section_present"), bool):
        section_present = prior_state["section_present"]
    else:
        section_present = section is not None
    if section is None:
        section = {}
        config[COMPACTION_SECTION] = section

    managed: dict[str, dict[str, Any]] = {}
    target_values = derived.config_values
    for key in MANAGED_COMPACTION_KEYS:
        new_value = target_values[key]
        present = key in section
        current = section.get(key)
        if owns(prior_state, key, config):
            # 仍是我們寫的值 —— 保留最初記下的「接管前原值」,不要被自己的
            # 上一次寫入蓋掉,否則切回 native 會還原成 CodeTrail 的值。
            prior = prior_state["managed"][key]["prior"]
        else:
            prior = _prior_record(present, current)
            if present and not json_equal(current, new_value):
                warnings.append(
                    f"{COMPACTION_SECTION}.{key} 原本是 {current!r},"
                    f"已由 CodeTrail 接管為 {new_value!r}(選 native 模式可還原)"
                )
        if not present or not json_equal(current, new_value):
            section[key] = new_value
            changes.append(f"{COMPACTION_SECTION}.{key} = {new_value!r}")
        managed[key] = {"prior": prior, "value": new_value}

    plugin_entry: dict[str, Any] = {"registered": False, "prior_present": False}
    if plugin_path is not None:
        p_changes, p_warnings, p_errors, plugin_entry = apply_plugin_entry(
            config,
            plugin_path=plugin_path,
            prior_state=prior_state if still_owned else None,
            register=True,
        )
        if p_errors:
            return changes, warnings, p_errors, None
        changes.extend(p_changes)
        warnings.extend(p_warnings)

    state = build_state(
        mode=mode,
        config_path=config_path,
        managed=managed,
        plugin=plugin_entry,
        section_present=section_present,
    )
    return changes, warnings, [], state


def _restore_native(
    config: dict[str, Any],
    *,
    prior_state: dict[str, Any] | None,
    plugin_path: Path | None,
    config_path: Path,
) -> tuple[list[str], list[str], list[str], dict[str, Any] | None]:
    """還原,並回一份 ``mode: native`` 的狀態。

    為什麼還原完還要留狀態檔:「明確選了 native」與「從來沒設定過」對
    contract check 是兩件事(前者不得再把 plugin 補回去);而且下一次接管時,
    ``section_present`` 與 plugin 的 ``prior_present`` 仍然要沿用最初記下的
    那一份 —— 那是接管前的事實,不會因為還原過一次就改變。
    """
    changes: list[str] = []
    warnings: list[str] = []
    section, error = _section(config)
    if error is not None:
        return [], [], [error], None

    for key in MANAGED_COMPACTION_KEYS:
        entry = (prior_state or {}).get("managed", {}).get(key)
        if not isinstance(entry, dict):
            continue
        if not owns(prior_state, key, config):
            if isinstance(section, dict) and key in section:
                warnings.append(
                    f"{COMPACTION_SECTION}.{key} 目前的值不是 CodeTrail 寫的"
                    "(你或專案設定改過),還原時原封不動"
                )
            continue
        prior = entry["prior"]
        assert isinstance(section, dict)  # owns() 已保證這個鍵存在
        if prior.get("present"):
            section[key] = prior["value"]
            changes.append(f"{COMPACTION_SECTION}.{key} 還原為 {prior['value']!r}")
        else:
            section.pop(key, None)
            changes.append(f"{COMPACTION_SECTION}.{key} 移除(接管前原本不存在)")

    section_present = bool((prior_state or {}).get("section_present", True))
    if (
        not section_present
        and isinstance(section, dict)
        and not section
        and COMPACTION_SECTION in config
    ):
        config.pop(COMPACTION_SECTION)
        changes.append(f"{COMPACTION_SECTION} 區塊移除(接管前原本不存在)")

    plugin_entry: dict[str, Any] = {
        "registered": False,
        "prior_present": bool(
            (prior_state or {}).get(PLUGIN_SECTION, {}).get("prior_present", False)
        ),
    }
    if plugin_path is not None:
        p_changes, p_warnings, p_errors, plugin_entry = apply_plugin_entry(
            config, plugin_path=plugin_path, prior_state=prior_state, register=False
        )
        if p_errors:
            return changes, warnings, p_errors, None
        changes.extend(p_changes)
        warnings.extend(p_warnings)
    state = build_state(
        mode=MODE_NATIVE,
        config_path=config_path,
        managed={},
        plugin=plugin_entry,
        section_present=section_present,
    )
    return changes, warnings, [], state


# ---------------------------------------------------------------------------
# plugin 陣列
# ---------------------------------------------------------------------------
def plugin_spec_text(spec: Any) -> str | None:
    """從 plugin 陣列的一筆取出路徑字串(字串或 ``[路徑, options]``)。"""
    if isinstance(spec, str):
        return spec.strip()
    if isinstance(spec, list) and spec and isinstance(spec[0], str):
        return spec[0].strip()
    return None


def plugin_local_path(text: str) -> str | None:
    """正規化成本機絕對路徑;不是本機絕對路徑就回 None。

    與 `scripts/opencode_contract_check._plugin_local_path` 同一條判準(有
    測試對照):OpenCode 會把裸絕對路徑正規化成 ``file:///…`` 再載入,只認
    其中一種形式的話,每次修復都會再 append 一筆,plugin 被載入兩次。
    """
    if not isinstance(text, str) or not text:
        return None
    if text.startswith("file://"):
        parts = urlsplit(text)
        if parts.netloc not in ("", "localhost"):
            return None
        return os.path.abspath(unquote(parts.path))
    scheme, sep, _ = text.partition(":")
    if sep and scheme and scheme[0].isalpha() and all(
        ch.isalnum() or ch in "+.-" for ch in scheme
    ):
        return None  # npm: / https: 之類的遠端 plugin,不是本機檔
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        return None
    return os.path.abspath(expanded)


def _matches(spec: Any, target: str) -> bool:
    text = plugin_spec_text(spec)
    if text is None:
        return False
    local = plugin_local_path(text)
    return local is not None and local == target


def plugin_path_hash(path: str) -> str:
    """註冊路徑的身分雜湊。狀態檔不放明文路徑,只放這個。"""
    return hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:32]


def _same_named_local(spec: Any) -> str | None:
    """同名的**本機**壓縮 plugin 路徑(可能是搬家前的舊 repo 位置)。

    只比對 exact target 的話,repo 從 /old/CodeTrail 搬到 /new/CodeTrail 之後
    會 append 新路徑而留下舊的:舊檔還在就載入兩個 instance,舊檔不在就讓
    整個 OpenCode instance 起不來。切回 native 也只會移除新那一筆。
    """
    text = plugin_spec_text(spec)
    if text is None:
        return None
    local = plugin_local_path(text)
    if local is None or os.path.basename(local) != PLUGIN_FILENAME:
        return None
    return local


def apply_plugin_entry(
    config: dict[str, Any],
    *,
    plugin_path: Path,
    prior_state: dict[str, Any] | None,
    register: bool,
) -> tuple[list[str], list[str], list[str], dict[str, Any]]:
    """把壓縮 plugin 加入 / 移出 ``config["plugin"]``。

    註冊時收斂成**恰好一筆**,而且優先保留帶 options 的 ``[路徑, options]``
    形式 —— 固定留第一筆會把使用者設的 options 丟掉。

    ownership 只由 ``state.plugin.path_hash`` 表示:**它就是「CodeTrail 自己
    寫下去的那一筆的路徑雜湊」**,`None` 代表我們一筆都不擁有(接管時陣列裡
    已經有一筆同 target 的,那是使用者的)。`prior_present` 只是 baseline
    (接管前有沒有同名項),不當 ownership 用 —— 兩者混在一起的話,「接管前
    就有舊路徑、搬家後我們另外加了一筆」會變成兩筆都不敢刪。

    baseline 本身也不是永久的:上一份狀態是 ``native`` 時(代表我們什麼都
    不擁有了),`prior_present` 與 `section_present` 會以**現況**重記 ——
    使用者在 native 期間自己加回來的東西是他的。

    移除只認 ``path_hash`` 指的那一筆,而且必須是裸字串(使用者事後加上
    options 的就不是我們寫的形狀了),其餘一律保留並警告。
    """
    target = os.path.abspath(str(plugin_path))
    if PLUGIN_SECTION in config:
        entries = config[PLUGIN_SECTION]
        if not isinstance(entries, list):
            shown = "null" if entries is None else type(entries).__name__
            return [], [], [f"plugin 必須是 JSON array,得到 {shown}"], {}
    else:
        entries = []
        if register:
            config[PLUGIN_SECTION] = entries

    changes: list[str] = []
    warnings: list[str] = []
    prior_plugin = (prior_state or {}).get(PLUGIN_SECTION)
    # 「同名但路徑不同」只有在**我們自己註冊過**的情況下才算搬家(舊 repo 路徑)。
    # 沒有 ownership 證據時它可能是使用者自己 fork 的同名 plugin —— 把它改寫成
    # 本 repo 路徑等於接管了一個不屬於我們的項目,而且切回 native 之後也還不回去。
    owned_hash = (
        prior_plugin.get("path_hash")
        if isinstance(prior_plugin, dict) and prior_plugin.get("registered") is True
        else None
    )
    exact = [index for index, spec in enumerate(entries) if _matches(spec, target)]
    # 「同名但路徑不同」只有在它**正好是我們上次註冊的那個路徑**時才算搬家。
    # 只看「我們註冊過某個東西」的話,使用者事後刪掉我們那筆、換上自己的
    # `/custom/codetrail-compaction.js`,下一次 --fix 就會把他的項改寫成本
    # repo 路徑,而狀態檔完全沒有證據說那筆是我們寫的。
    owned_elsewhere = (
        [
            index for index, spec in enumerate(entries)
            if index not in exact
            and (_same_named_local(spec) is not None)
            and plugin_path_hash(_same_named_local(spec)) == owned_hash
        ]
        if owned_hash
        else []
    )
    if owned_elsewhere:
        matches = sorted(exact + owned_elsewhere)
    else:
        matches = list(exact)
        foreign = [
            index for index, spec in enumerate(entries)
            if _same_named_local(spec) is not None and index not in exact
        ]
        if foreign:
            warnings.append(
                "plugin 陣列裡有同名但路徑不同的本機 plugin"
                f"({plugin_spec_text(entries[foreign[0]])});CodeTrail 沒有註冊過它,"
                "所以不動它。兩個同名 plugin 會同時被載入,不要的那個請自己移除"
            )
    prior_present = (
        bool(prior_plugin.get("prior_present"))
        if isinstance(prior_plugin, dict)
        else bool(matches)
    )
    # 這一次之前我們有沒有擁有其中一筆(而不是「有沒有註冊過任何東西」)。
    owned_now = bool(owned_elsewhere) or (
        bool(owned_hash) and any(
            plugin_path_hash(_same_named_local(entries[i]) or "") == owned_hash
            for i in exact
        )
    )

    if register:
        if not matches:
            entries.append(target)
            changes.append(f"plugin 註冊 CodeTrail 壓縮 plugin:{target}")
            return changes, warnings, [], {
                "registered": True,
                "prior_present": prior_present,
                "entry": "string",
                # **我們剛剛自己寫下去的**,所以留路徑證據 —— 即使接管前使用者
                # 另外有一筆(那筆是他的,由 prior_present 記著)。
                "path_hash": plugin_path_hash(target),
            }
        # 優先留已經指向 target 的那一筆(它可能帶著使用者設的 options);
        # 其次留任何帶 options 的同名項,最後才是第一筆。
        keep = next(
            (index for index in exact if isinstance(entries[index], list)),
            next(
                (index for index in exact),
                next(
                    (index for index in matches if isinstance(entries[index], list)),
                    matches[0],
                ),
            ),
        )
        # 先把留下來的那一筆抓在手上:pop 掉前面的重複項之後 `keep` 的索引就
        # 位移了,再用它去取值會拿到隔壁那筆不相干的 plugin。
        kept_spec = entries[keep]
        if not _matches(kept_spec, target):
            old_text = plugin_spec_text(kept_spec)
            entries[keep] = (
                [target, *kept_spec[1:]] if isinstance(kept_spec, list) else target
            )
            kept_spec = entries[keep]
            changes.append(f"plugin 路徑更新(repo 搬家):{old_text} → {target}")
        for index in sorted((i for i in matches if i != keep), reverse=True):
            removed = plugin_spec_text(entries[index])
            entries.pop(index)
            changes.append(f"plugin 移除重複的同名壓縮 plugin 項:{removed}")
        # 留下來的那一筆是不是我們的:之前就擁有它(含搬家前的舊路徑)才算。
        # 接管時陣列裡已經有一筆同 target 的,那是使用者的,不能認領。
        return changes, warnings, [], {
            "registered": True,
            "prior_present": prior_present,
            "entry": "list" if isinstance(kept_spec, list) else "string",
            "path_hash": plugin_path_hash(target) if owned_now else None,
        }

    if owned_hash is None:
        # 我們一筆都不擁有(接管時那筆本來就在)。什麼都不移除。
        if matches:
            warnings.append(
                "plugin 陣列裡的壓縮 plugin 不是 CodeTrail 寫下去的(接管前就存在),保留不動"
            )
        return changes, warnings, [], {
            "registered": False,
            "prior_present": prior_present,
            "entry": (prior_plugin or {}).get("entry", "string"),
            "path_hash": None,
        }
    # 只移除 `path_hash` 指的那一筆(可能停在搬家前的舊路徑)。同名但不是它的
    # 項是使用者的,一律不動;帶 options 的也不是我們寫的形狀。
    ours = sorted(
        index for index in matches
        if plugin_path_hash(_same_named_local(entries[index]) or "") == owned_hash
        or _matches(entries[index], target)
        and owned_hash == plugin_path_hash(target)
    )
    removable = [index for index in ours if isinstance(entries[index], str)]
    kept = [index for index in ours if index not in removable]
    for index in sorted(removable, reverse=True):
        entries.pop(index)
        changes.append("plugin 移除 CodeTrail 壓縮 plugin(native 模式)")
    if kept:
        warnings.append(
            "plugin 陣列裡的壓縮 plugin 帶著你設定的 options,不是 CodeTrail 寫的形狀,保留不動"
        )
    if not kept and isinstance(config.get(PLUGIN_SECTION), list) and not config[PLUGIN_SECTION]:
        config.pop(PLUGIN_SECTION)
        changes.append("plugin 陣列已空,移除該欄位")
    return changes, warnings, [], {
        "registered": bool(kept),
        "prior_present": prior_present,
        "entry": "list" if kept else "string",
        "path_hash": owned_hash if kept else None,
    }


# ---------------------------------------------------------------------------
# 有效設定核對
# ---------------------------------------------------------------------------
def _plugin_was_pre_existing(state: dict[str, Any] | None) -> bool:
    """接管前使用者自己就載入了這個 plugin 嗎?

    是的話,native 模式下它還在陣列裡是**正確**的(ownership 邏輯刻意保留它),
    不能報成漂移 —— 否則每個 session 都會跳一次錯誤 toast 並寫一筆 incident。
    """
    plugin = (state or {}).get(PLUGIN_SECTION)
    return isinstance(plugin, dict) and plugin.get("prior_present") is True


def effective_drift(
    config: dict[str, Any],
    *,
    state: dict[str, Any] | None,
    plugin_path: Path | None = None,
) -> list[str]:
    """回報「有效設定與狀態檔記載的模式不一致」的每一條。

    空 list = 一致。plugin 在 runtime 用同樣的判準(自己讀 ``config.get()``),
    不一致就停用自動動作並 fail-loud —— 絕不在 runtime 偷改使用者的設定。
    """
    if not state:
        return []
    mode = state.get("mode")
    drift: list[str] = []
    section, error = _section(config)
    if error is not None:
        return [error]
    if mode in PLUGIN_MODES:
        for key, entry in state.get("managed", {}).items():
            expected = entry.get("value")
            present = isinstance(section, dict) and key in section
            shown = repr(section[key]) if present else "(不存在)"
            if not present or not json_equal(section[key], expected):
                drift.append(
                    f"{COMPACTION_SECTION}.{key} 有效值是 {shown},"
                    f"模式 {mode} 需要 {expected!r}"
                )
    if plugin_path is not None:
        target = os.path.abspath(str(plugin_path))
        entries = config.get(PLUGIN_SECTION)
        registered = isinstance(entries, list) and any(
            _matches(spec, target) for spec in entries
        )
        if mode in PLUGIN_MODES and not registered:
            drift.append("plugin 陣列缺少 CodeTrail 壓縮 plugin")
        if mode == MODE_NATIVE and registered and not _plugin_was_pre_existing(state):
            drift.append("native 模式但 plugin 陣列仍載入 CodeTrail 壓縮 plugin")
    return drift
