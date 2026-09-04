#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opencode_migrate — 舊 OpenCode 安裝的一次性遷移。

CodeTrail 不再啟動 OpenCode,但**曾經**寫過幾個值進使用者的 OpenCode 設定:
壓縮的四個受管 ``compaction.*`` 鍵,以及兩個 plugin 項。留著不管的後果不是
「多一點沒用的設定」,而是:

  * ``compaction.auto = false`` 會一直生效 —— 那是 CodeTrail 接管時關掉的。
    不還原的話,使用者的 OpenCode 從此不再自動壓縮,而且沒有人負責。
  * plugin 項指向這個 repo 的檔案路徑。這兩個檔一旦被刪掉,使用者在**其他
    專案**開 OpenCode 都會因為載不到 plugin 而起不來。

所以遷移是「**還原** + 撤銷註冊」,不是「刪掉一堆鍵」。還原邏輯就是這個模組
下半部的 native 路徑(:func:`apply_mode` 帶 ``mode="native"``):它的 ownership
語意(只還原現值仍等於 CodeTrail 寫入值的鍵)與那一整組安全測試都還在。

**這個檔是 runtime 之外的東西**。`aicode` / `codetrail_chat` / `mcp_server` /
`set_config` / `client_*` 一個都不 import 它;它只在使用者手動執行
``python3 opencode_migrate.py`` 時跑。門檻公式那一半住在 `compaction_formula`,
而那一半不知道 OpenCode 的存在。

這裡只多做三件 native 路徑沒有的事:
  1. 一併移除**通知** plugin 項(native 只處理壓縮 plugin);
  2. 完成後**刪除**狀態檔,而不是寫一份 ``mode: native`` 的;
  3. 偵測自訂 instructions / 全域 AGENTS.md 並印出提示 —— **絕不自動搬**。

``mcp.codetrail`` 與 ``permission`` 一律不動:``mcp_server.py`` 仍然可以被任何
MCP client 用,拿掉等於替使用者決定他不能再這樣用。

沒有狀態檔、也沒有 CodeTrail plugin 項的機器:**一個 byte 都不動**。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from compaction_formula import (  # noqa: E402
    TAIL_TURNS,
    UPSTREAM_COMPACTION_BUFFER,
    UPSTREAM_MIN_PRESERVE_RECENT_TOKENS,
    UPSTREAM_OUTPUT_TOKEN_MAX,
    CompactionModeError,
    DerivedSettings,
    combine_settings,
    derive_settings,
    effective_max_output,
)

# ===========================================================================
# 以下整段來自舊的 compaction_mode.py:opencode.json 的 ownership 那一半。
#
# 為什麼搬進這裡:它處理的全部是**使用者的 OpenCode 設定** —— 受管鍵、
# ownership 狀態檔、plugin 項、native 還原。runtime 一個都用不到,留在共用模組
# 只會讓「main 不碰 OpenCode」這條界線靠自律維持。公式那一半在
# `compaction_formula`,而那一半不 import 這裡。
# ===========================================================================
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
#: opencode.json 的 ``compaction`` 物件裡由 CodeTrail 擁有的鍵 —— 接管時會寫、
#: 切回 native 時會還原的那一組。
MANAGED_COMPACTION_KEYS = ("auto", "tail_turns", "preserve_recent_tokens", "prune")
#: 受管鍵裡「值不符 = 壓縮契約破了」的那一組。
#:
#: 為什麼要跟 ``MANAGED_COMPACTION_KEYS`` 分開:漂移偵測的後果是**停用那個
#: session 的自動壓縮並跳錯誤 toast**。``auto`` / ``tail_turns`` /
#: ``preserve_recent_tokens`` 被改掉,觸發點與逐字保留的範圍就跟推導出來的
#: 不一樣,再壓下去會失真 —— 停用是對的。``prune`` 不是:它只決定舊工具輸出
#: 佔多少 context,關掉之後壓縮照樣正確,只是對話長得比較快。為它停掉整個
#: session 的壓縮不成比例。
#:
#: 這條分界順帶讓「新增受管鍵」不再是破壞性升級:舊安裝的狀態檔沒有新鍵,
#: 而新鍵不在契約集合裡,所以 ``git pull`` 之後不會每個 session 都跳
#: config_drift。要納入管理得重跑 ``./set_config.sh``(見 ``unmanaged_keys``)。
CONTRACT_COMPACTION_KEYS = ("auto", "tail_turns", "preserve_recent_tokens")
#: ``compaction.prune`` 的受管值。上游預設 false(1.18.21 的 schema 註解逐字
#: 寫著 default: false)。開了之後,超過最近兩個 user turn 的舊工具輸出在累積
#: 到上游 ``PRUNE_PROTECT``(40000 tokens)之上、而且可修剪的量超過
#: ``PRUNE_MINIMUM``(20000 tokens)時,送進模型的那一份會被換成
#: ``[Old tool result content cleared]``。**只換送進模型的那一份** —— 上游只在
#: part 上補一個 ``state.time.compacted`` 時間戳,DB 裡的原文與 TUI 顯示不動。
#:
#: 為什麼要開:CodeTrail 的工具結果預算是 context 的 12%,一段對話累積數十萬
#: 字元的工具輸出是常態,而那些輸出在被摘要之前會一直佔著 context。
#: 代價寫清楚:被清掉的工具結果進不了下一次摘要的「已確定事實」,模型要再看
#: 就得重叫工具。這是刻意的取捨,不是沒想到。
PRUNE_OLD_TOOL_OUTPUT = True
COMPACTION_SECTION = "compaction"
PLUGIN_SECTION = "plugin"
MIN_COMPACTION_OPENCODE_VERSION = (1, 18, 17)


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


def managed_values(derived: DerivedSettings) -> dict[str, Any]:
    """要寫進 ``opencode.json`` 的那四個受管值。

    這是 opencode.json 的形狀,所以住在這裡而不是 `compaction_formula`:
    runtime 只要門檻與 tail 保留額,不需要知道鍵叫什麼。
    """
    return {
        "auto": False,
        "tail_turns": TAIL_TURNS,
        "preserve_recent_tokens": derived.preserve_recent_tokens,
        "prune": PRUNE_OLD_TOOL_OUTPUT,
    }


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

    所以只接受**純量**,而且字串限 ASCII。受管鍵(`auto` / `tail_turns` /
    `preserve_recent_tokens` / `prune`)在上游 schema 裡本來就是 boolean /
    number,容器或非 ASCII 字串都不是有意義的值,擋掉不構成實務限制。
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


def unmanaged_keys(state: dict[str, Any] | None) -> tuple[str, ...]:
    """這一版新增、但這份狀態檔還沒接管的受管鍵。

    受管鍵的集合會隨版本長大,而狀態檔只記得**接管當下**那幾個。那些新鍵
    CodeTrail 沒有任何 ownership 證據,所以不會去寫它們 —— 寫了就等於在使用者
    沒有授權的情況下接管一個鍵,而且切回 native 時還原不回去。

    但也不能靜靜當作沒這回事:使用者 `git pull` 之後永遠拿不到新的受管值,
    而且沒有任何訊息說為什麼。所以由呼叫端(doctor、`aicode` 橫幅)把這個
    清單講出來,收斂方式一律是重跑 ``./set_config.sh``。

    native 模式回空 tuple:那條路徑本來就什麼都不管。
    """
    if not state or state.get("mode") not in PLUGIN_MODES:
        return ()
    managed = state.get("managed")
    if not isinstance(managed, dict):
        return ()
    return tuple(key for key in MANAGED_COMPACTION_KEYS if key not in managed)


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
    target_values = managed_values(derived)
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
            # `prune` 之類的非契約鍵改掉不會讓壓縮失真,不報成漂移 ——
            # 報了就等於為一個「只影響 context 用量」的鍵停掉整個 session。
            if key not in CONTRACT_COMPACTION_KEYS:
                continue
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



BACKUP_SUFFIX = ".codetrail-migrate.bak"

#: 這兩個檔名曾經被註冊進使用者的 opencode.json。
COMPACTION_PLUGIN = PLUGIN_FILENAME
NOTIFY_PLUGIN = "codetrail-notify.js"
PLUGIN_DIR = REPO_ROOT / "opencode_plugins"

#: CodeTrail 曾經自己加進 instructions 的唯一一項。
OWN_INSTRUCTION = ".codetrail/lessons.md"


def opencode_config_candidates(env: Mapping[str, str] | None = None) -> list[Path]:
    """使用者的 opencode.json 可能在哪裡。

    這段原本住在 `model_resolution`。搬過來的理由:主模型解析鏈已經完全不看
    OpenCode 的設定,留在那裡等於 runtime 仍然「認得」那個檔;而這裡是唯一
    還需要讀它的地方(手動升級工具)。

    非預設位置走 **argv**(`--config <path>`,呼叫端經 `env` 傳進來的
    ``OPENCODE_CONFIG`` 鍵)。刻意不讀 `os.environ`:這支是**唯一**會寫使用者
    OpenCode 設定的路徑,而殼層裡一個殘留的 `OPENCODE_CONFIG` 會讓它去改另一
    份設定 —— 那正是這整個模組的 ownership 契約要防的事。
    """
    environ = env or {}
    explicit = (environ.get("OPENCODE_CONFIG") or "").strip()
    if explicit:
        return [Path(explicit).expanduser()]
    home = environ.get("HOME") or environ.get("USERPROFILE")
    if home:
        return [Path(home) / ".config" / "opencode" / "opencode.json"]
    return []


def _load_first_opencode_config(
    env: Mapping[str, str] | None = None,
) -> tuple[Path | None, dict | None, str]:
    environ = env or {}
    explicit = bool((environ.get("OPENCODE_CONFIG") or "").strip())
    for path in opencode_config_candidates(environ):
        try:
            if not path.is_file():
                if explicit:
                    return path, None, "--config 指定的檔案不存在。"
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return path, None, str(exc)
        if not isinstance(data, dict):
            return path, None, "OpenCode config root must be a JSON object."
        return path, data, ""
    return None, None, ""


class MigrationError(RuntimeError):
    """遷移無法安全進行(設定讀不到、狀態檔對不上、寫入失敗)。"""


@dataclass
class MigrationPlan:
    config_path: Path | None = None
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    removed_plugins: list[str] = field(default_factory=list)
    state_path: Path | None = None
    state_present: bool = False
    notes: list[str] = field(default_factory=list)
    #: 接管紀錄是**別份安裝**寫的(狀態檔記的 plugin 路徑不是這個 checkout)。
    #: 這時什麼都不做:那份接管歸寫它的那個 checkout 管,拿這裡的路徑去比對
    #: ownership 會把「不是我們寫的」判成「使用者手改過」,然後一個鍵都還原不了、
    #: 卻把狀態檔刪掉 —— 真正的擁有者從此還原不回去。
    foreign_owner: str = ""
    _config: dict[str, Any] | None = None

    @property
    def needed(self) -> bool:
        if self.foreign_owner:
            return False
        return bool(self.changes or self.removed_plugins or self.state_present)

    def render(self) -> list[str]:
        lines: list[str] = []
        if self.foreign_owner == FOREIGN_UNVERIFIABLE:
            return [
                "接管紀錄記的 plugin 不是這一份 CodeTrail 的,而 OpenCode 設定裡已經沒有"
                "那個 plugin 項,無法確認是哪一份安裝寫的;這裡一個 byte 都不動。",
                "  如果那一份安裝還在,請到它的 checkout 執行 `python3 opencode_migrate.py`;",
                "  如果確定就是這一份搬過家,請先把 OpenCode 設定裡的 plugin 項補回舊路徑"
                "(狀態檔只記雜湊),或手動移除 ~/.config/codetrail/compaction.json"
                "(移除等於放棄自動還原,舊值要自己改回)。",
            ]
        if self.foreign_owner:
            lines.append(
                f"接管紀錄是另一份 CodeTrail 安裝寫的({self.foreign_owner} 還在);"
                "這裡一個 byte 都不動。"
            )
            lines.append(
                "  要解除那一份的接管,請到寫它的那個 checkout 執行 "
                "`python3 opencode_migrate.py`。"
            )
            lines.extend(f"  {note}" for note in self.notes)
            return lines
        if not self.needed:
            lines.append("沒有需要遷移的東西(沒有狀態檔,也沒有 CodeTrail 註冊的 plugin 項)。")
            lines.extend(self.notes)
            return lines
        if self.config_path:
            lines.append(f"OpenCode 設定: {self.config_path}")
        lines.extend(f"  {change}" for change in self.changes)
        lines.extend(f"  取消註冊 plugin {spec}" for spec in self.removed_plugins)
        if self.state_present and self.state_path:
            lines.append(f"  刪除 ownership 狀態檔 {self.state_path}")
        lines.extend(f"  ⚠ {warning}" for warning in self.warnings)
        lines.extend(f"  {note}" for note in self.notes)
        return lines


def _load_config(env: Mapping[str, str]) -> tuple[Path | None, dict[str, Any]]:
    path, value, error = _load_first_opencode_config(env)
    if error:
        raise MigrationError(f"讀不到 OpenCode 設定: {error}")
    if path is None or not isinstance(value, dict):
        return None, {}
    return path, value


def _plugin_entries(config: Mapping[str, Any], filename: str) -> list[str]:
    """只認**本 repo 路徑**的 plugin 項。

    同名但指向別處的一律不碰:那是別人的 plugin,不是我們註冊的。
    """
    raw = config.get(PLUGIN_SECTION)
    if not isinstance(raw, list):
        return []
    target = str((PLUGIN_DIR / filename).resolve())
    found: list[str] = []
    for item in raw:
        text = plugin_spec_text(item)
        if not text:
            continue
        local = plugin_local_path(text)
        if local is None:
            continue
        try:
            if str(Path(local).resolve()) == target:
                found.append(text)
        except OSError:
            continue
    return found


def _notify_plugin_entries(config: Mapping[str, Any]) -> list[str]:
    return _plugin_entries(config, NOTIFY_PLUGIN)


def _compaction_plugin_entries(config: Mapping[str, Any]) -> list[str]:
    """壓縮 plugin 的殘留項。

    `apply_mode` 走的是「沒有狀態檔 = 沒有接管」的 fail-closed 語意,所以
    **值**不會被動;但 plugin 項是 path 對得上的,那就是我們註冊的,和有沒有
    狀態檔無關。少了這條,一台只剩 plugin 項的機器永遠不會被提示遷移,
    而那個 plugin 會一直在 OpenCode 裡跑。
    """
    return _plugin_entries(config, COMPACTION_PLUGIN)


def _custom_instruction_notes(config: Mapping[str, Any]) -> list[str]:
    notes: list[str] = []
    instructions = config.get("instructions")
    if isinstance(instructions, list):
        custom = [
            item
            for item in instructions
            if isinstance(item, str) and OWN_INSTRUCTION not in item
        ]
        if custom:
            notes.append(
                "OpenCode 的 instructions 還有自訂項,CodeTrail **不會**自動搬: "
                + ", ".join(custom)
                + "。要讓 CodeTrail 客戶端也載入的話,自己貼進 "
                "~/.config/codetrail/instructions.md。"
            )
    agents = Path(
        os.environ.get("HOME", str(Path.home()))
    ) / ".config" / "opencode" / "AGENTS.md"
    if agents.is_file():
        notes.append(
            f"全域 {agents} 仍在。CodeTrail 客戶端有自己的內建基底規則,不會讀它;"
            "有自訂內容的話同樣可以貼進 ~/.config/codetrail/instructions.md。"
        )
    return notes


def plan_migration(env: Mapping[str, str] | None = None) -> MigrationPlan:
    """算出要做什麼。**不寫任何檔。**"""
    environ = dict(os.environ if env is None else env)
    plan = MigrationPlan()

    ownership_state = state_path(environ)
    plan.state_path = ownership_state
    state = None
    try:
        state = load_state(ownership_state, environ)
    except Exception as exc:  # noqa: BLE001
        if ownership_state.exists() or ownership_state.is_symlink():
            # 狀態檔**在**卻讀不了(壞掉、權限不對、symlink):這不是「沒接管過」。
            # 它記的正是還原用的原值;吞成 warning 等於讓那些舊值永遠沒人還原,
            # 而且 aicode 從此不再提示。fail-loud,讓使用者處理那個檔。
            raise MigrationError(
                f"ownership 狀態檔存在但無法信任({exc}):{ownership_state}。"
                "請修好或移除它(移除等於放棄自動還原,舊值要自己改回)之後再重跑。"
            ) from exc
        plan.warnings.append(f"ownership 狀態檔讀不到({exc});只會處理 plugin 項。")
    if state is None and (ownership_state.exists() or ownership_state.is_symlink()):
        # load_state 對壞掉 / 權限過寬 / symlink 的狀態檔是 fail-closed 回 None
        # (「沒有可信的接管紀錄」),但檔案**在**就不是「沒接管過」:見上面。
        raise MigrationError(
            f"ownership 狀態檔存在但無法信任:{ownership_state}。"
            "請修好或移除它(移除等於放棄自動還原,舊值要自己改回)之後再重跑。"
        )
    plan.state_present = state is not None

    config_path, config = _load_config(environ)
    plan.config_path = config_path

    foreign = _foreign_plugin_owner(state, config)
    if foreign:
        # 別份安裝的接管:零寫入、只提示。
        plan.foreign_owner = foreign
        return plan
    if _recorded_plugin_is_not_ours(state) and not _config_names_recorded_plugin(state, config):
        # 狀態檔記的 plugin 不是本 repo 的,而 OpenCode 設定裡**找不到**那個項
        # (設定不在 / 空物件 / 項被人工移除)—— 反推不出擁有者是誰。「本 repo 搬過
        # 家」與「別份安裝」在這裡分不開;分不開就是無法確認,無法確認就零寫入。
        # 以前這條路會提早回傳,而 `state_present` 又讓 migration 被判成 needed,
        # 最後把狀態檔刪掉:真正擁有者永久失去還原 prior 值的證據。
        plan.foreign_owner = FOREIGN_UNVERIFIABLE
        return plan
    if not config or config_path is None:
        return plan

    working = json.loads(json.dumps(config))  # 深拷貝,plan 階段不動原件
    plugin_path = PLUGIN_DIR / COMPACTION_PLUGIN
    changes, warnings, errors, _state = apply_mode(
        working,
        mode=MODE_NATIVE,
        derived=None,
        prior_state=state,
        config_path=config_path,
        plugin_path=plugin_path if plugin_path.is_file() else None,
    )
    if errors:
        raise MigrationError("; ".join(errors))
    plan.changes = list(changes)
    plan.warnings.extend(warnings)

    plan.removed_plugins = _notify_plugin_entries(working) + [
        spec for spec in _compaction_plugin_entries(working)
        # apply_mode 已經處理過的不重複列。
        if not any(spec in change for change in plan.changes)
    ]
    for spec in plan.removed_plugins:
        entries = working.get(PLUGIN_SECTION)
        if isinstance(entries, list):
            working[PLUGIN_SECTION] = [
                item
                for item in entries
                if plugin_spec_text(item) != spec
            ]
    entries = working.get(PLUGIN_SECTION)
    if isinstance(entries, list) and not entries and not _plugin_existed_before(state):
        working.pop(PLUGIN_SECTION, None)

    plan.notes = _custom_instruction_notes(working)
    plan._config = working
    return plan


def _foreign_plugin_owner(
    state: Mapping[str, Any] | None, config: Mapping[str, Any] | None
) -> str:
    """狀態檔記的 plugin 是**另一份還在的安裝**寫的嗎?是就回它的路徑。

    同一台機器可以有兩份 CodeTrail(舊 OpenCode 世代的 checkout 與這一份),
    而 ownership 狀態檔只有一個位置。拿這裡的路徑去跑還原,會把另一份寫進去的值
    判成「使用者手改過」而全部不還原,然後照樣把狀態檔刪掉 —— 那一份從此還原
    不回去,而且沒有任何訊息。

    判準不能只是「雜湊不等於我」:**這個 repo 搬過家**時記的也是舊路徑,而那條
    路徑既有的更新邏輯(``plugin 路徑更新(repo 搬家)``)是要保住的。所以再多問
    一句「那個路徑現在還在不在」:
      * 還在 → 那是另一份活著的安裝,零寫入、只提示;
      * 不在 → 就是本 repo 的舊位置,照舊走搬家那條路。
    """
    entry = (state or {}).get(PLUGIN_SECTION)
    if not isinstance(entry, dict) or not entry.get("registered"):
        return ""
    recorded_hash = entry.get("path_hash")
    if not isinstance(recorded_hash, str) or not recorded_hash:
        return ""
    if recorded_hash == plugin_path_hash(str(PLUGIN_DIR / COMPACTION_PLUGIN)):
        return ""
    entries = (config or {}).get(PLUGIN_SECTION)
    if not isinstance(entries, list):
        return ""
    for spec in entries:
        text = plugin_spec_text(spec)
        local = plugin_local_path(text) if text else None
        if not local or plugin_path_hash(local) != recorded_hash:
            continue
        return local if Path(local).is_file() else ""
    return ""


#: `MigrationPlan.foreign_owner` 的特殊值:確定不是我們的,但反推不出是誰的。
FOREIGN_UNVERIFIABLE = "(無法確認擁有者:OpenCode 設定裡已經沒有那個 plugin 項)"


def _recorded_plugin_is_not_ours(state: Mapping[str, Any] | None) -> bool:
    entry = (state or {}).get(PLUGIN_SECTION)
    if not isinstance(entry, dict) or not entry.get("registered"):
        return False
    recorded_hash = entry.get("path_hash")
    if not isinstance(recorded_hash, str) or not recorded_hash:
        return False
    return recorded_hash != plugin_path_hash(str(PLUGIN_DIR / COMPACTION_PLUGIN))


def _config_names_recorded_plugin(
    state: Mapping[str, Any] | None, config: Mapping[str, Any] | None
) -> bool:
    """OpenCode 設定裡有沒有一個 plugin 項對得上狀態檔記的雜湊(有才反推得出路徑)。"""
    entry = (state or {}).get(PLUGIN_SECTION)
    recorded_hash = entry.get("path_hash") if isinstance(entry, dict) else None
    entries = (config or {}).get(PLUGIN_SECTION)
    if not isinstance(recorded_hash, str) or not isinstance(entries, list):
        return False
    for spec in entries:
        text = plugin_spec_text(spec)
        local = plugin_local_path(text) if text else None
        if local and plugin_path_hash(local) == recorded_hash:
            return True
    return False


def _plugin_existed_before(state: Mapping[str, Any] | None) -> bool:
    entry = (state or {}).get(PLUGIN_SECTION)
    return bool(isinstance(entry, dict) and entry.get("prior_present"))


def apply_migration(env: Mapping[str, str] | None = None, *, dry_run: bool = False) -> MigrationPlan:
    """執行遷移。有備份;寫檔失敗時狀態檔不動。"""
    environ = dict(os.environ if env is None else env)
    plan = plan_migration(environ)
    if dry_run or not plan.needed:
        return plan

    if plan.config_path is not None and plan._config is not None and (
        plan.changes or plan.removed_plugins
    ):
        _write_config(plan.config_path, plan._config)

    if plan.state_present and plan.state_path is not None:
        if not clear_state(plan.state_path, environ):
            # 狀態檔還在 = 下一次還會被判定成「仍在接管」,而設定已經還原了。
            raise MigrationError(
                f"設定已還原,但 ownership 狀態檔刪不掉:{plan.state_path}。"
                "請手動移除它,否則下一次啟動仍會提示遷移。"
            )
    return plan


def _write_config(path: Path, config: Mapping[str, Any]) -> None:
    """原子替換並留備份。symlink 一律解析到真檔再寫(沿用既有寫入語意)。"""
    real = path.resolve()
    try:
        backup = real.with_name(real.name + BACKUP_SUFFIX)
        shutil.copy2(real, backup)
        # 原本的權限一定要留住:這份設定可能含 provider API key,而一般
        # `umask 022` 下新建的 temp file 是 0644 —— replace 之後就等於把一份
        # 0600 的機密設定放寬成全機器可讀,而且完全無聲。
        mode = real.stat().st_mode & 0o7777
        tmp = real.with_name(f".{real.name}.codetrail-migrate.tmp")
        tmp.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except OSError as exc:
        raise MigrationError(f"無法寫入 {real}: {exc}") from exc


def migration_problem(env: Mapping[str, str] | None = None) -> str:
    """遷移狀態**判不出來**時的原因;判得出來回空字串。

    狀態檔壞掉、綁到別份 config、OpenCode 設定解析不了,都不是「不需要遷移」:
    舊值與 plugin 項可能還在使用者的設定裡沒人管。這種情況要講出來,不能吞成
    False 讓 `aicode` 從此不再提示。
    """
    try:
        plan_migration(env)
    except MigrationError as exc:
        return str(exc)
    return ""


def migration_needed(env: Mapping[str, str] | None = None) -> bool:
    """`aicode` preflight 用:只偵測,不寫檔、不擋啟動。

    判不出來也算「需要看一眼」(True):見 :func:`migration_problem`。
    """
    try:
        return plan_migration(env).needed
    except MigrationError:
        return True


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只回報,不寫任何檔")
    parser.add_argument(
        "--config",
        default="",
        help="非預設位置的 opencode.json(預設 ~/.config/opencode/opencode.json)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    # 只交這支真的需要的東西:HOME(定位預設路徑與狀態檔)與 argv 指定的目標。
    env = {
        name: value
        for name in ("HOME", "USERPROFILE", "XDG_STATE_HOME")
        if (value := os.environ.get(name))
    }
    if args.config.strip():
        env["OPENCODE_CONFIG"] = args.config.strip()
    try:
        plan = apply_migration(env, dry_run=args.check)
    except MigrationError as exc:
        print(f"[migrate] {exc}", file=sys.stderr)
        return 2
    for line in plan.render():
        print(f"[migrate] {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
