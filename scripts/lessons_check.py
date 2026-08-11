#!/usr/bin/env python3
"""session start 的 lessons 注入 + 複審提示(aicode preflight 用)。

做三件事:
  1. 載入 per-deployment 的 lessons store(~/.config/codetrail/lessons.json)。
  2. 把「scope 相符且未過 review_by」的 active lessons render 成
     <AICODE_ROOT>/.codetrail/lessons.md —— OpenCode 經 opencode.json 的
     "instructions": [".codetrail/lessons.md"] 把它跟 AGENTS.md 一起載入
     context(set_config.sh 會補這個欄位)。沒有 active lessons 也會寫檔
     (內容標明為空),避免 instructions 指到不存在的檔案。
  3. 已過 review_by 的 lessons 停止注入,並在這裡醒目列出待複審清單
     (renew / delete 指令)。

呼叫方式 (aicode wrapper 用):
    python3 scripts/lessons_check.py --root /path/to/project

讀取的環境變數:
    AICODE_LESSONS_FILE    lessons.json 覆寫路徑(測試 / 進階)
    AICODE_LESSONS_SKIP    =1 時整個檢查跳過(緊急逃生;不 render、不提示)

退出碼:
    0  OK(含「有過期待複審」的情況 —— 過期是正常生命週期,只提示不擋)
    2  store 損壞 / active 超過上限 / 無法寫 context file → wrapper 應 abort

設計守則:
- fail-loud:store 壞掉不靜默略過 —— 帶著壞 store 啟動會讓使用者以為
  lessons 有生效。超過上限不做「只注入前 20 條」的 silent truncation。
- 過期 → 停止注入 + 醒目提示,但不擋啟動:過期是時間自然發生的,
  不是使用者設定錯誤,擋下來等於懲罰沒天天複審的人。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import lessons  # noqa: E402


def _print(line: str) -> None:
    print(f"[lessons] {line}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CodeTrail lessons session-start check")
    parser.add_argument("--root", required=True, help="AICODE_ROOT(專案根目錄)")
    args = parser.parse_args(argv)

    if os.environ.get("AICODE_LESSONS_SKIP", "").lower() in ("1", "true", "yes"):
        _print("WARN: 已用 AICODE_LESSONS_SKIP 跳過 lessons 注入(本 session 不套用行為教訓)")
        return 0

    try:
        root = Path(args.root).resolve()
    except OSError as exc:
        _print(f"--root 無法解析: {exc}")
        return 2
    if not root.is_dir():
        _print(f"--root 不是目錄: {args.root}")
        return 2

    try:
        store_path = lessons.default_lessons_path()
        data = lessons.load_lessons(store_path)
    except lessons.LessonsError as exc:
        _print(f"lessons store 損壞,refuse to start: {exc}")
        _print(f"store 路徑: 見 ${lessons.LESSONS_FILE_ENV} 或 ~/.config/codetrail/lessons.json")
        _print("緊急跳過(本 session 不注入): AICODE_LESSONS_SKIP=1 aicode")
        return 2

    today = lessons.today_utc()
    active = lessons.active_lessons(data, str(root), today)
    expired = lessons.expired_lessons(data, str(root), today)

    # 上限保險絲:add/renew 已在寫入時拒絕超額,這裡只可能因手改 JSON 觸發。
    # 依 fail-loud 政策拒絕啟動,不做「只注入前 N 條」的 silent truncation。
    if len(active) > lessons.LESSONS_MAX_ACTIVE:
        _print(
            f"active lessons {len(active)} 條,超過上限 {lessons.LESSONS_MAX_ACTIVE},"
            "refuse to start。"
        )
        _print("請人工整併:python lessons.py list 檢視,delete 或合併到剩餘條數 ≤ 上限。")
        _print("緊急跳過(本 session 不注入): AICODE_LESSONS_SKIP=1 aicode")
        return 2

    try:
        target = lessons.write_context_file(root, active)
    except OSError as exc:
        _print(f"無法寫入 lessons context file: {exc}")
        return 2

    if active:
        _print(f"{len(active)} 條 active lessons 已注入 {target.relative_to(root)}")
    else:
        _print(f"沒有 active lessons(已寫空的 {target.relative_to(root)})")

    if expired:
        _print(f"⚠ {len(expired)} 條 lessons 已過 review_by,本 session 起停止注入,待人工複審:")
        for lesson in expired:
            _print(f"  - {lesson['id']} (review_by={lesson['review_by']}) {lesson['rule']}")
        _print("  複審通過 → python lessons.py renew <id>   淘汰 → python lessons.py delete <id>")

    return 0


if __name__ == "__main__":
    sys.exit(main())
