#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_policy — 工具權限 policy(可替換,不是一張固定表)。

兩個實作:
  * ``InteractivePolicy`` —— 終端 / web 用。唯讀工具 allow;六個寫入工具 ask,
    核准框完整顯示參數(含整份 patch)。拒絕以 tool error 回給模型,重問有上限。
  * ``ReadOnlyPolicy`` —— canary、routing eval、session_eval replay 用。
    凡 ``tools/list`` 註記**不是** ``readOnlyHint`` 的工具一律 deny。

為什麼 deny 判準看的是 ``tools/list`` 的註記而不是一張寫死的名單:新增一個
寫入工具而忘了加進名單時,寫死的名單會**放行**它。註記是 server 自己宣告的,
新工具沒有 ``readOnlyHint=True`` 就自動被 deny。名單仍然存在,但只當
**下限**(測試釘住:這八個一定要被 deny),不是唯一判準。
"""
from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any, Protocol

#: 互動模式需要人工核准的工具。
ASK_TOOLS: frozenset[str] = frozenset(
    {
        "apply_patch",
        "run_lint",
        "run_command",
        "remove_document",
        "record_lesson",
        "review_figures",
        "review_text",
        # `client.json` 的 `external_import` 把授權從「單次啟動」變成**跨專案
        # 持久**,所以實際動作要逐次確認:核准框會顯示來源與目的路徑。
        # 開關一開就自動放行,等於使用者只能在事後從檔案系統發現模型複製了什麼。
        "import_external_file",
    }
)

#: 這幾個工具的 ask **不是偏好,是邊界**:`client.json` 的 `permission` 可以把它們
#: 收緊成 deny,但不得放寬成 allow。`import_external_file` 的開關授權的是「可以從
#: 專案外複製檔案進來」這件事,每一次的來源與目的仍要人看過(plan §6 第 12 條)。
NEVER_AUTO_ALLOWED: frozenset[str] = frozenset({"import_external_file"})

#: readonly policy 必須 deny 的 mutator 下限。
#: 真正的判準是 ``readOnlyHint``;這份名單只是「至少這些」的測試錨點。
MUTATING_TOOLS: frozenset[str] = ASK_TOOLS | {"ingest_document"}

#: 同一輪裡同一個被拒絕的工具最多讓模型重問幾次。
MAX_DENIED_RETRIES = 2


class Decision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionPolicy(Protocol):
    name: str

    def decide(self, tool_name: str, *, read_only: bool, arguments: Mapping[str, Any]) -> Decision:
        ...


class InteractivePolicy:
    """互動預設:唯讀 allow、六個寫入工具 ask、其餘非唯讀一律 ask。"""

    name = "interactive"

    def __init__(self, ask_tools: frozenset[str] = ASK_TOOLS) -> None:
        self.ask_tools = frozenset(ask_tools)

    def decide(self, tool_name: str, *, read_only: bool, arguments: Mapping[str, Any]) -> Decision:
        if tool_name in self.ask_tools:
            return Decision.ASK
        # 其餘一律 allow —— 包含 ingest_document / reload_knowledge_base
        # 這幾個「不是唯讀但也不需要每次問」的工具。
        # 這與舊世代前端的權限表逐條相同(`codetrail_*: allow` 再把六個覆成
        # ask),不是放寬:改成「非唯讀就 ask」會讓一次 ingest 多跳一個核准框,
        # 那是使用者沒要求過的行為改變。
        #
        # 代價講明:**互動模式**下新增的寫入工具預設是 allow,要人工核准就得
        # 加進 ASK_TOOLS。fail-closed 的那一半在 ReadOnlyPolicy —— 評測 / 抽查
        # 用的是它,而它的判準是 server 宣告的 readOnlyHint,新工具漏加名單也
        # 一樣被 deny。
        return Decision.ALLOW


class ReadOnlyPolicy:
    """canary / eval / replay:凡不是 readOnlyHint 的工具一律 deny。"""

    name = "readonly"

    def decide(self, tool_name: str, *, read_only: bool, arguments: Mapping[str, Any]) -> Decision:
        return Decision.ALLOW if read_only else Decision.DENY


def denial_message(tool_name: str, policy_name: str) -> str:
    """回給模型的 tool error 文字。

    刻意講清楚「這是權限,不是工具壞掉」:講成錯誤的話模型會重試同一個呼叫。
    """
    if policy_name == "readonly":
        return (
            f"status: error\n"
            f"permission denied: {tool_name} 在唯讀模式下不可用。\n"
            "next: 這是評測 / 抽查用的唯讀 session,寫入與執行工具全部停用。"
            "改用唯讀工具取得證據,或直接說明需要哪一個寫入動作。"
        )
    return (
        f"status: error\n"
        f"permission denied: 使用者拒絕了這次 {tool_name} 呼叫。\n"
        "next: 不要重試同一個呼叫。改問使用者要怎麼做,或改用唯讀工具取得證據。"
    )


class OverridePolicy:
    """使用者在 ``client.json`` 明寫的每個工具決定,蓋在 base policy 上。

    刻意**不**讓覆寫放寬 readonly policy:唯讀 session(canary / eval / replay)
    的 deny 是評測邊界,不是偏好。覆寫只作用在互動 policy 上,而且 `deny`
    永遠贏 —— 使用者關掉一個工具就是關掉。
    """

    def __init__(self, base: PermissionPolicy, overrides: Mapping[str, str]) -> None:
        self.base = base
        self.overrides = {str(k): str(v) for k, v in overrides.items()}
        self.name = base.name

    def decide(self, tool_name: str, *, read_only: bool, arguments: Mapping[str, Any]) -> Decision:
        decision = self.base.decide(tool_name, read_only=read_only, arguments=arguments)
        override = self.overrides.get(tool_name)
        if override is None:
            return decision
        if decision is Decision.DENY:
            # readonly policy 的 deny 是邊界,不接受放寬。
            return decision
        try:
            wanted = Decision(override)
        except ValueError:
            return decision
        if wanted is Decision.ALLOW and tool_name in NEVER_AUTO_ALLOWED:
            # 只准收緊。放寬這一個等於 client.json 一行就拆掉「每次匯入都要人看」。
            return decision
        return wanted
