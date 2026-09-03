#!/usr/bin/env python3
"""目前的 OpenCode 壓縮模式——給 `aicode_opencode` 啟動橫幅用的幾行摘要。

為什麼要印這個:壓縮模式決定「長對話什麼時候被換成一段摘要」,但它只在
OpenCode **啟動時** 生效,選擇又記在 `~/.config/codetrail/compaction.json`
——沒有人會每次去 cat 它。結果是使用者可能整段 session 都在用 CodeTrail 的
壓縮而不自知,`codetrail` / `manual` 又還在測試階段。

契約(兩條,都是刻意的):

  * **純讀取**。這裡不寫任何檔、不修任何設定。要改模式只有
    `./set_config.sh --compaction-mode ...` 一條路。
  * **永遠 exit 0、永不 raise**。這是啟動橫幅的一段資訊,不是閘。讀不到狀態
    檔就退成「未接管」——讓一行資訊擋住 OpenCode 啟動是本末倒置。

顯示的模式與 runtime 實際生效的模式必須是同一個來源:這裡讀的是
`compaction_mode.inspect_state()`(plugin、contract check、doctor 讀的也是它),
不是另外猜一份。plugin 會停用的每一種情況——有效設定與模式對不上、狀態檔綁在
另一份 opencode.json、OpenCode 版本低於壓縮語意的下限——這裡都要講,而且後面
那幾行也要跟著改口:只印「壓縮模式=codetrail」而實際上什麼都沒做,比不印還糟。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: 與 opencode_plugins/codetrail-compaction.js 的 `KEEP_REASONING_ENV` 逐字相同。
KEEP_REASONING_ENV = "CODETRAIL_KEEP_REASONING"
#: 同上,plugin 的 `OPENCODE_VERSION_ENV`。`aicode_opencode` 的 preflight 量到之後遞下來,
#: 沒有這個變數 = 不是 aicode_opencode 起的 session,plugin 那端也不做版本判斷。
OPENCODE_VERSION_ENV = "AICODE_OPENCODE_VERSION"
#: plugin `parseVersion()` 的 Python 對應 —— 認得的形狀要一模一樣,否則兩邊會
#: 對同一個字串給出不同的支援與否。
_VERSION_RE = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)\s*$")

SWITCH_HINT = (
    "行為仍在調整;要回 OpenCode 原生行為:./set_config.sh --compaction-mode native"
)


def _version_lines(cm, env: dict) -> tuple[bool | None, list[str]]:
    """OpenCode 版本支不支援這一版的壓縮語意 —— 回 (支援?, 要顯示的行)。

    `None` = 不知道(沒有經過 `aicode_opencode` preflight,plugin 那端同樣不判斷)。
    量得到而且太舊時 plugin **整個**停用(壓縮與歷史 reasoning 兩邊都不做),
    所以這裡非講不可:不講的話畫面只寫「模式=codetrail」,而 runtime 什麼
    都沒做。
    """
    raw = str(env.get(OPENCODE_VERSION_ENV) or "").strip()
    match = _VERSION_RE.match(raw)
    if match is None:
        return None, []
    minimum = tuple(cm.MIN_COMPACTION_OPENCODE_VERSION)
    if tuple(int(part) for part in match.groups()) >= minimum:
        return True, []
    return False, [
        f"⚠ OpenCode {raw} 低於 {'.'.join(map(str, minimum))},壓縮 plugin 會整個"
        "停用(那之前的訊息序列化語意不同);升級 OpenCode,"
        "或 ./set_config.sh --compaction-mode native"
    ]


def _config_lines(cm, state: dict, env: dict) -> tuple[bool | None, list[str]]:
    """有效設定那一半:身分/漂移警告 + 目前的觸發門檻。

    回 (狀態檔是不是綁在目前有效的那份 config, 要顯示的行);`None` = 讀不到
    設定,無從得知。身分對不上時 plugin 直接當成「沒有接管」,所以那個結果
    要回給呼叫端 —— 其他行也不能再照著「已接管」的口徑寫。

    門檻要印出來的理由:短對話永遠不會壓縮(這是對的),但畫面上只寫「模式=
    codetrail」的話,使用者無從分辨「還沒到門檻」與「plugin 根本沒載入」。

    讀設定失敗一律當成「沒話說」:這一段是加值資訊,不能變成新的失敗來源。
    """
    try:
        import model_resolution

        path, config, error = model_resolution.load_first_opencode_config(env)
        if error or path is None or not isinstance(config, dict):
            return None, []
        if not cm.state_matches_config(state, path):
            return False, [
                f"⚠ 狀態檔記錄的是另一份 opencode.json(目前有效的是 {path});"
                "壓縮 plugin 會當成沒有接管而完全不動作,"
                "對這一份重跑 ./set_config.sh 才會對得起來"
            ]
        lines = []
        if cm.effective_drift(config, state=state, plugin_path=cm.PLUGIN_PATH):
            lines.append(
                "⚠ 有效設定與記錄的模式不一致,壓縮 plugin 會停用自動壓縮;"
                "跑 python3 scripts/doctor.py 看是哪一個鍵"
            )
        try:
            derived = cm.derive_for_config(config)
        except cm.CompactionModeError as exc:
            return True, [*lines, f"⚠ 門檻算不出來:{exc}"]
        if derived is not None:
            settings = derived[0]
            lines.append(
                f"idle 門檻={settings.idle_threshold} tokens、"
                f"tail 保留={settings.preserve_recent_tokens} tokens"
                "(對話還沒到門檻就不會壓縮,那是正常的)"
            )
        return True, lines
    except Exception:  # noqa: BLE001 — 一行資訊不得因為任何讀取問題而中斷
        return None, []


def _reasoning_line(env: dict, active: bool | None) -> list[str]:
    """舊回合的 reasoning 有沒有被丟掉——那是最大的一筆 context 差異。

    為什麼要印:plugin 會把「最新一則使用者訊息之前」的 assistant reasoning
    從送進模型的訊息裡拿掉(每段對話省下三到五成的成長)。這偏離 DeepSeek-V4
    官方模板「有 tools 就全留」的行為,是模型相依的品質取捨——使用者至少要
    知道自己在哪一邊,以及關掉它的那個變數叫什麼。

    `active` 是「transform 這個 hook 真的會跑嗎」。它有三道閘,這裡三道都要
    看齊:逃生口(下面那個環境變數)、版本、以及有效的模式狀態。只看逃生口
    的話,版本太舊或狀態檔綁在另一份 config 時畫面照樣寫「不進模型」,而
    整段歷史 reasoning 其實原封不動地送進去——使用者以為 context 已經縮了,
    真的撞到上限時還會照著這一行去找錯方向。漂移(`effective_drift`)刻意
    不算:那停的是自動壓縮,transform 本來就不看它。
    """
    raw = str(env.get(KEEP_REASONING_ENV) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return [f"舊回合 reasoning=保留({KEEP_REASONING_ENV} 已設定)"]
    if active is False:
        return ["舊回合 reasoning=照送(壓縮 plugin 未生效,見上面的 ⚠)"]
    if active is None:
        return ["舊回合 reasoning=未知(讀不到有效設定,無法確認 plugin 會不會生效)"]
    return [
        "舊回合 reasoning=不進模型(省 context;"
        f"要保留就設 {KEEP_REASONING_ENV}=1)"
    ]


def _pending_managed_lines(cm, state: dict) -> list[str]:
    """這一版新增、但狀態檔還沒接管的受管鍵。

    CodeTrail 對這些鍵沒有 ownership 證據,所以不會自己補(補了就代表在沒有
    授權的情況下接管,而且切回 native 還原不回去)。不講的話使用者升級之後
    永遠拿不到新受管值,而且沒有任何訊息。
    """
    try:
        pending = cm.unmanaged_keys(state)
    except Exception:  # noqa: BLE001 — 一行資訊不得變成新的失敗來源
        return []
    if not pending:
        return []
    keys = "、".join(f"{cm.COMPACTION_SECTION}.{key}" for key in pending)
    return [f"這一版新增了受管值({keys}),重跑 ./set_config.sh 才會生效"]


def status_lines(env: dict | None = None) -> list[str]:
    """回要顯示的行(第一行是摘要,其餘是補充)。任何情況都回得出東西。"""
    values = dict(os.environ if env is None else env)
    try:
        import compaction_mode as cm
    except Exception as exc:  # noqa: BLE001
        return [f"壓縮模式=未知(compaction_mode 不可用:{exc});OpenCode 維持原生行為"]

    try:
        state, reason = cm.inspect_state(path=cm.state_path(values))
    except Exception as exc:  # noqa: BLE001
        return [f"壓縮模式=native(讀不到狀態檔:{exc};CodeTrail 未接管)"]

    if reason:
        # 狀態檔存在但不可信(權限/symlink/內容)。plugin 用同一個判準,所以
        # 這種情況 runtime 也是「沒有接管」——照實說,不要顯示上一次的選擇。
        return [
            f"壓縮模式=native(狀態檔已忽略:{reason};CodeTrail 未接管)",
            "重跑 ./set_config.sh 可以重新選擇壓縮模式",
        ]
    if state is None:
        return ["壓縮模式=native(CodeTrail 未接管,OpenCode 原生行為)"]

    mode = state.get("mode")
    label = cm.MODE_LABELS.get(mode, str(mode))
    if not cm.is_experimental(mode):
        return [f"壓縮模式={mode}({label})"]

    lines = [f"壓縮模式={mode} {cm.EXPERIMENTAL_TAG}——{label}"]
    version_ok, version_lines = _version_lines(cm, values)
    identity_ok, config_lines = _config_lines(cm, state, values)
    lines.extend(version_lines)
    lines.extend(config_lines)
    # plugin 的每一個 hook 都先過「版本 + 有效的模式狀態」這兩道;其中任何
    # 一道擋下來,runtime 就什麼都不做,後面的行不得再照「已接管」的口徑寫。
    if version_ok is False or identity_ok is False:
        active: bool | None = False
    elif identity_ok is True:
        active = True
    else:
        active = None
    lines.extend(_reasoning_line(values, active))
    lines.extend(_pending_managed_lines(cm, state))
    if mode == cm.MODE_MANUAL:
        # manual 靠使用者自己按 /compact,而那是完整 TUI 才有的指令:--mini 會把
        # 它當成一般訊息送給模型(模型還會回「好的,開始壓縮」),
        # `opencode run --command compact` 直接回 Command not found。
        lines.append("/compact 只有完整 TUI 有效(--mini 與 opencode run 都不支援)")
    # 第一行已經帶 EXPERIMENTAL_TAG,這裡不再重複那個 emoji —— 兩行都掛 🧪
    # 只會讓人略過第二行,而第二行才是那條還原命令。
    lines.append(SWITCH_HINT)
    return lines


def render(lines: list[str], prefix: str = "") -> str:
    """第一行帶 prefix,後續行對齊到 prefix 之後(aicode_opencode 橫幅的既有排版)。"""
    if not prefix:
        return "\n".join(lines)
    pad = " " * (len(prefix) + 1)
    return "\n".join(
        f"{prefix} {line}" if index == 0 else f"{pad}{line}"
        for index, line in enumerate(lines)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="印出目前的 OpenCode 壓縮模式(純讀取,永遠 exit 0)"
    )
    parser.add_argument(
        "--prefix", default="",
        help="每行前綴(例:'[aicode_opencode]');後續行自動對齊到前綴之後",
    )
    args = parser.parse_args([] if argv is None else argv)
    try:
        lines = status_lines()
    except Exception as exc:  # noqa: BLE001 — main 是最後一道 fail-open
        lines = [f"壓縮模式=未知({exc});OpenCode 維持原生行為"]
    print(render(lines, args.prefix), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
