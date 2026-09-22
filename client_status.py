#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_status — 目前的壓縮模式那幾行(`aicode` 啟動橫幅與 `codetrail_chat.py status`)。

原本是 `scripts/compaction_status.py`;plan 要它併進客戶端,所以現在住在這裡,
`codetrail_chat.py status --prefix '[aicode]'` 是唯一入口。

為什麼要印這個:壓縮模式決定「長對話什麼時候被換成一段摘要」,選擇記在
`~/.config/codetrail/client.json`——沒有人會每次去 cat 它。結果是使用者可能
整段 session 都在用結構化壓縮而不自知,`codetrail` / `manual` 又還在測試階段。

契約(兩條,都是刻意的):

  * **純讀取**。這裡不寫任何檔、不修任何設定。要改模式請設定
    `client.json` 的 `compaction_mode` 為 `codetrail` / `manual` / `off`，重啟 aicode 生效。
  * **永遠 exit 0、永不 raise**。這是啟動橫幅的一段資訊,不是閘。讀不到設定
    檔就退成「未接管(manual)」——讓一行資訊擋住客戶端啟動是本末倒置。

顯示的模式與 runtime 實際生效的模式必須是同一個來源:這裡讀的是
`client_config.load_client_settings()`(engine 讀的也是它),不是另外猜一份。
每一種會讓 runtime 什麼都不做的情況——設定檔不可信、n_ctx 推不出門檻——這裡
都要講,而且後面那幾行也要跟著改口:只印「壓縮模式=codetrail」而實際上什麼
都沒做,比不印還糟。
"""
from __future__ import annotations

import argparse
import os
import sys

SWITCH_HINT = (
    "行為仍在調整;要完全關掉壓縮:將 ~/.config/codetrail/client.json 的 "
    '"compaction_mode" 設為 "off"，重啟 aicode 後生效。'
)

MODE_LABELS = {
    "codetrail": "助理答完、對話進 idle 之後自動壓縮",
    "manual": "只有你自己按 /compact 時才壓縮",
    "off": "完全不壓縮(context 滿了會是可見的錯誤)",
}


def _threshold_lines(cc, n_ctx: int | None) -> tuple[bool | None, list[str]]:
    """門檻那一半:算得出來就印,算不出來就講明 runtime 不會壓縮。

    門檻要印出來的理由:短對話永遠不會壓縮(這是對的),但畫面上只寫
    「模式=codetrail」的話,使用者無從分辨「還沒到門檻」與「根本推不出門檻」。
    """
    if not n_ctx:
        return None, ["idle 門檻=未知(這一輪還沒解析出 n_ctx)"]
    try:
        derived = cc.derive(n_ctx)
    except Exception as exc:  # noqa: BLE001 — 一行資訊不得變成新的失敗來源
        return False, [
            f"⚠ 這個 n_ctx({n_ctx})推不出可用的壓縮門檻,runtime 不會壓縮:{exc}"
        ]
    return True, [
        f"idle 門檻={derived.idle_threshold} tokens、"
        f"tail 保留={derived.preserve_recent_tokens} tokens"
        "(對話還沒到門檻就不會壓縮,那是正常的)"
    ]


def _reasoning_line(settings) -> list[str]:
    """舊回合的 reasoning 有沒有被丟掉——那是最大的一筆 context 差異。

    為什麼要印:客戶端會把「最新一則真實使用者訊息之前」的 assistant
    reasoning 從送進模型的訊息裡拿掉(每段對話省下三到五成的成長)。這偏離
    DeepSeek-V4 官方模板「有 tools 就全留」的行為,是模型相依的品質取捨——
    使用者至少要知道自己在哪一邊,以及要改哪個鍵。

    來源是 `client.json` 的 `keep_historical_reasoning`(以前是一個環境變數)。
    與 `show_reasoning` 是**兩個**鍵:那個只管畫面,這個管送模 payload。
    與壓縮模式**無關**:客戶端一律生效。
    """
    if getattr(settings, "keep_historical_reasoning", False):
        return ['舊回合 reasoning=保留(client.json 的 "keep_historical_reasoning": true)']
    return [
        "舊回合 reasoning=不進模型(省 context;"
        '要保留就在 client.json 設 "keep_historical_reasoning": true)'
    ]


def _stopped_line(cc, env: dict) -> list[str]:
    """有沒有 session 因為摘要不可信而被永久停用。"""
    try:
        stopped = cc.read_stopped(env)
    except Exception:  # noqa: BLE001
        return []
    if not stopped:
        return []
    return [
        f"有 {len(stopped)} 個舊 session 因摘要不可信而停用了自動壓縮"
        "(紀錄在 compaction-stopped.jsonl;刪掉那個檔就清空)"
    ]


def status_lines(env: dict | None = None, *, n_ctx: int | None = None) -> list[str]:
    """回要顯示的行(第一行是摘要,其餘是補充)。任何情況都回得出東西。"""
    values = dict(os.environ if env is None else env)
    try:
        import client_compaction as cc
        import client_config
    except Exception as exc:  # noqa: BLE001
        return [f"壓縮模式=未知(客戶端模組不可用:{exc})"]

    try:
        settings = client_config.load_client_settings(values)
    except Exception as exc:  # noqa: BLE001
        # 設定檔存在但不可信(權限/symlink/內容)。engine 用同一個判準,所以
        # 這種情況 runtime 也起不來——照實說,不要顯示上一次的選擇。
        return [
            f"壓縮模式=未知(client.json 不可信:{exc})",
            "請先修復 client.json 的擁有者、權限、連結或 JSON 格式問題，"
            '再將 "compaction_mode" 設為 "codetrail"、"manual" 或 "off"；重啟 aicode 後生效。',
        ]

    mode = settings.compaction_mode
    label = MODE_LABELS.get(mode, mode)
    if not settings.present:
        return [
            f"壓縮模式={mode}(沒有 {settings.path};CodeTrail 未接管)",
            '壓縮設定在 client.json 的 "compaction_mode"：自動壓縮設 "codetrail"、'
            '手動設 "manual"、關閉設 "off"；設定方式見 docs/compaction-rules.md，重啟 aicode 後生效。',
            *_reasoning_line(settings),
        ]

    lines = [f"壓縮模式={mode} 🧪 實驗中——{label}"]
    if mode != cc.MODE_OFF:
        # n_ctx **優先用呼叫端觀測到的真值**(preflight 讀主 server 的 /props);
        # 沒給才退回 deployment profile 的設定值。門檻是拿這個數字推出來的,
        # 而 Engine 用的是觀測值 —— 兩邊不同就會印出一個沒有人在用的門檻,
        # 那正是這一行存在的反面。
        observed = n_ctx
        if not observed:
            try:
                import config as _config

                observed = int(getattr(_config, "N_CTX", 0) or 0) or None
            except Exception:  # noqa: BLE001 - 橫幅不得因為設定讀不到而消失
                observed = None
        _ok, threshold_lines = _threshold_lines(cc, observed)
        lines.extend(threshold_lines)
    lines.extend(_reasoning_line(settings))
    lines.extend(_stopped_line(cc, values))
    if settings.permission:
        overrides = "、".join(f"{k}={v}" for k, v in sorted(settings.permission.items()))
        lines.append(f"權限覆寫:{overrides}")
    if mode == cc.MODE_MANUAL:
        lines.append("/compact 只在 aicode 的終端介面有效(headless run 不壓縮)")
    if mode == cc.MODE_OFF:
        lines.append("context 滿了會是一個可見的錯誤,不會自動補救")
    else:
        lines.append(SWITCH_HINT)
    return lines


def render(lines: list[str], prefix: str = "") -> str:
    """第一行帶 prefix,後續行對齊到 prefix 之後(aicode 橫幅的既有排版)。"""
    if not prefix:
        return "\n".join(lines)
    pad = " " * (len(prefix) + 1)
    return "\n".join(
        f"{prefix} {line}" if index == 0 else f"{pad}{line}"
        for index, line in enumerate(lines)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="印出目前的壓縮模式(純讀取,永遠 exit 0)"
    )
    parser.add_argument(
        "--prefix", default="",
        help="每行前綴(例:'[aicode]');後續行自動對齊到前綴之後",
    )
    args = parser.parse_args([] if argv is None else argv)
    try:
        lines = status_lines()
    except Exception as exc:  # noqa: BLE001 — main 是最後一道 fail-open
        lines = [f"壓縮模式=未知({exc})"]
    print(render(lines, args.prefix), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - 入口是 codetrail_chat.py status
    raise SystemExit(main(sys.argv[1:]))
