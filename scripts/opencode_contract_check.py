#!/usr/bin/env python3
"""Check or repair the CodeTrail-managed contract fields in OpenCode config.

舊安裝 ``git pull`` 之後,mcp_server 立刻暴露新工具、aicode 開始 render
lessons 注入檔,但全域 opencode.json 還停在舊範本,產生兩個升級破口:

  1. permission 缺新工具的 ask 覆寫 → 舊的 ``codetrail_*: allow`` wildcard
     直接放行(OpenCode 是 last-matching-rule-wins)。``record_lesson`` 這類
     「必須人工核准」的寫入工具就繞過了核准框 —— 違反 docs/lessons.md 的
     「沒有任何無審核的自動寫入路徑」。
  2. instructions 缺 ``.codetrail/lessons.md`` → lessons render 了也不會被
     OpenCode 載入,啟動輸出卻顯示「已注入」。

``aicode`` 每次啟動用 ``--fix`` 呼叫這裡,比照 opencode_mcp_timeout_check:
只在既有 mcp.codetrail 設定存在時動作(那是「這份 config 由 CodeTrail 管」
的訊號)、只補「缺少」的鍵 —— 使用者明確設過的值一律尊重、只警告 ——
原子寫入並保留備份。要整組重建請重跑 ./set_config.sh。

補鍵位置說明:新鍵一律 append 在 permission 物件最後,JSON object 保序 +
last-matching-rule-wins,所以必定蓋過前面的 ``codetrail_*`` wildcard。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 沿用同一套 config 定位(OPENCODE_CONFIG 覆寫)、讀取與備份命名,避免兩個
# preflight 對「opencode.json 在哪」各說各話。
from scripts.opencode_mcp_timeout_check import (  # noqa: E402
    BACKUP_SUFFIX as BACKUP_SUFFIX,  # re-export:測試與呼叫端取備份路徑用
)
from scripts.opencode_mcp_timeout_check import (  # noqa: E402
    _codetrail_entry,
    _next_backup_path,
    _read_config,
    _truthy,
    resolve_config_path,
)

SKIP_ENV = "AICODE_OPENCODE_CONTRACT_CHECK_SKIP"

# 必須維持 ask 人工核准的 CodeTrail 寫入類工具。跟 set_config.py 的
# _OPENCODE_PERMISSION_TEMPLATE 保持同步(tests 有 cross-check 釘住)。
REQUIRED_ASK_TOOLS = (
    "codetrail_apply_patch",
    "codetrail_run_lint",
    "codetrail_run_command",
    "codetrail_remove_document",
    "codetrail_record_lesson",
)
LESSONS_INSTRUCTION = ".codetrail/lessons.md"


def _print(line: str) -> None:
    print(f"[oc-contract] {line}", flush=True)


def apply_contract(data: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """把缺少的契約鍵補進 data(in-place),回傳 (變更, 警告, 阻斷錯誤)。

    變更非空才需要寫檔;阻斷錯誤非空時呼叫端不得寫檔(型別壞掉的欄位交給
    使用者 / set_config.sh 處理,自動路徑不做整段重建)。
    """
    changes: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []

    permission = data.get("permission")
    if permission is None:
        data["permission"] = {tool: "ask" for tool in REQUIRED_ASK_TOOLS}
        changes.append(
            f"permission 原本不存在 → 補上 {len(REQUIRED_ASK_TOOLS)} 個 codetrail "
            "寫入工具的 ask 核准閘"
        )
        warnings.append(
            "permission 其餘建議(deny OpenCode 內建 bash/read/write 等)"
            "請重跑 ./set_config.sh 取得完整範本"
        )
    elif not isinstance(permission, dict):
        errors.append(f"permission 必須是 JSON object,得到 {type(permission).__name__}")
    else:
        added = []
        for tool in REQUIRED_ASK_TOOLS:
            if tool not in permission:
                # append 在最後 → last-matching-rule-wins 蓋過 codetrail_* wildcard
                permission[tool] = "ask"
                added.append(tool)
            elif permission[tool] != "ask":
                warnings.append(
                    f"permission.{tool}={permission[tool]!r}(建議 'ask',已尊重你的"
                    "設定;這代表該工具的寫入不經人工核准,見 docs/security.md)"
                )
        if added:
            changes.append("permission 補上 ask 核准閘:" + ", ".join(added))

    instructions = data.get("instructions")
    if instructions is None:
        data["instructions"] = [LESSONS_INSTRUCTION]
        changes.append(f"instructions 加入 '{LESSONS_INSTRUCTION}'(lessons 注入)")
    elif not isinstance(instructions, list):
        errors.append(f"instructions 必須是 JSON array,得到 {type(instructions).__name__}")
    elif LESSONS_INSTRUCTION not in instructions:
        instructions.append(LESSONS_INSTRUCTION)
        changes.append(
            f"instructions 補上 '{LESSONS_INSTRUCTION}'(lessons 注入;其他項目保留)"
        )

    return changes, warnings, errors


def _write_config(path: Path, data: dict[str, Any]) -> tuple[Path, Path]:
    """原子寫回並備份,回傳 (target, backup)。

    先 resolve 再 replace,OPENCODE_CONFIG 是 symlink 時保留 symlink 本身
    (比照 opencode_mcp_timeout_check 的寫入行為)。
    """
    target = path.resolve(strict=True)
    backup = _next_backup_path(target)
    shutil.copy2(target, backup)

    original_mode = stat.S_IMODE(target.stat().st_mode)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.codetrail-",
        suffix=".tmp",
        dir=target.parent,
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, original_mode)
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return target, backup


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check or repair CodeTrail ask-gates and lessons instructions "
        "in OpenCode config."
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="atomically add missing ask-gate permissions and the lessons "
        "instructions entry (backup kept)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args([] if argv is None else argv)
    if _truthy(os.environ.get(SKIP_ENV)):
        _print(f"skipped via {SKIP_ENV}=1")
        return 0

    path = resolve_config_path(dict(os.environ))
    if path is None:
        _print("UNKNOWN: 無法定位 opencode.json,跳過檢查")
        return 0

    data, error = _read_config(path)
    if error:
        _print(f"INVALID: {path}: {error}")
        return 2
    if data is None:
        _print(f"UNKNOWN: {path} 不存在,跳過檢查")
        return 0
    if _codetrail_entry(data) is None:
        _print(f"UNKNOWN: {path} 沒有 mcp.codetrail 設定,跳過檢查")
        return 0

    changes, warnings, errors = apply_contract(data)
    for warning in warnings:
        _print(f"⚠ {warning}")
    if errors:
        for item in errors:
            _print(f"INVALID: {item} ({path})")
        _print("           自動修復不重建型別壞掉的欄位;請手動修正,或重跑 ./set_config.sh")
        _print("           (會整組重建並備份原檔)。")
        return 2
    if not changes:
        _print(f"SAFE: ask 核准閘與 lessons instructions 都已就緒 ({path})")
        return 0

    if not args.fix:
        for item in changes:
            _print(f"MISSING: {item}")
        _print("           執行本腳本 --fix 自動補上(有備份),或重跑 ./set_config.sh。")
        _print(f"           緊急跳過(不建議): {SKIP_ENV}=1 aicode")
        return 2

    try:
        target, backup = _write_config(path, data)
    except OSError as exc:
        _print(f"FIX_FAILED: {path}: {type(exc).__name__}: {exc}")
        return 2
    for item in changes:
        _print(f"FIXED: {item}")
    _print(f"       目標: {target}")
    _print(f"       原設定備份: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
