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
import re
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
    "codetrail_review_figures",
)
LESSONS_INSTRUCTION = ".codetrail/lessons.md"

# 全域 AGENTS.md(OpenCode 每段對話自動載入的行為規則)的來源範本。
# set_config.py 不產生這份檔 —— 它一直只能靠使用者從文件複製貼上,所以
# `git pull` 之後它會靜默停在舊版:工具清單少了新工具,模型於是否認新工具
# 存在(那份清單就是最強的防幻覺錨點)。這裡負責把漂移講出來。
AGENTS_TEMPLATE_DOC = REPO_ROOT / "docs" / "opencode-agents-template.md"
AGENTS_MD_NAME = "AGENTS.md"
AGENTS_MD_SKIP_ENV = "AICODE_AGENTS_MD_CHECK_SKIP"

_AGENTS_FENCE_RE = re.compile(r"^```markdown\n(.*?)^```\s*$", re.S | re.M)
_TOOL_COUNT_RE = re.compile(r"CodeTrail 工具共\s*(\d+)\s*個")
_TOOL_NAME_RE = re.compile(r"`(codetrail_[a-z0-9_]+)`")


def _print(line: str) -> None:
    print(f"[oc-contract] {line}", flush=True)


class AgentsTemplateError(RuntimeError):
    """範本檔缺失或形狀不對 —— 不猜,直接說。"""


def extract_agents_template(text: str) -> str:
    """從 docs/opencode-agents-template.md 抽出唯一的 ```markdown fenced block。

    範本檔本身是「說明 + 一個 fenced block」;真正要裝進
    ``~/.config/opencode/AGENTS.md`` 的只有 block 內文。抓到 0 個或 2 個以上
    一律 raise:靜默挑第一個會在範本改版時裝錯內容。
    """
    blocks = _AGENTS_FENCE_RE.findall(text)
    if len(blocks) != 1:
        raise AgentsTemplateError(
            f"{AGENTS_TEMPLATE_DOC.name} 必須剛好有一個 ```markdown fenced block,"
            f"實得 {len(blocks)} 個"
        )
    return blocks[0]


def _tool_anchor(text: str) -> tuple[str | None, tuple[str, ...]]:
    """回傳 (宣告的工具數, 列出的工具名)。抓不到工具行時回 (None, ())。"""
    for line in text.splitlines():
        match = _TOOL_COUNT_RE.search(line)
        if match:
            return match.group(1), tuple(sorted(set(_TOOL_NAME_RE.findall(line))))
    return None, ()


def agents_md_status(live: str | None, template: str) -> tuple[str, list[str]]:
    """比對 live 與範本,回傳 (status, notes)。

    status:
      ``missing``  —— 沒有這份檔(從沒裝過)。
      ``ok``       —— 與範本逐字相同。
      ``stale``    —— **工具清單對不上**。這條會讓模型否認新工具存在,是真的會壞事。
      ``drifted``  —— 工具清單一致,其餘內容不同(使用者自訂,或範本改了說明性段落)。

    只有 ``missing`` 值得自動寫入;``drifted`` 可能是刻意的自訂,不得覆蓋。
    """
    if live is None:
        return "missing", []
    if live == template:
        return "ok", []

    live_count, live_tools = _tool_anchor(live)
    tpl_count, tpl_tools = _tool_anchor(template)
    notes: list[str] = []
    if live_tools != tpl_tools or live_count != tpl_count:
        if live_count is None:
            notes.append("live 的 AGENTS.md 沒有「CodeTrail 工具共 N 個」這一行")
        else:
            notes.append(f"工具數:live 寫 {live_count} 個,範本是 {tpl_count} 個")
        for name in sorted(set(tpl_tools) - set(live_tools)):
            notes.append(f"live 缺少工具:{name}")
        for name in sorted(set(live_tools) - set(tpl_tools)):
            notes.append(f"live 多出已不存在的工具:{name}")
        return "stale", notes

    live_lines = set(live.splitlines())
    absent = [ln for ln in template.splitlines() if ln.strip() and ln not in live_lines]
    notes.append(f"範本有 {len(absent)} 行不在 live 的 AGENTS.md 裡")
    return "drifted", notes


def _write_agents_md(target: Path, body: str) -> Path | None:
    """原子寫入 AGENTS.md,已存在則先備份。回傳備份路徑(新裝時為 None)。

    比照 ``_write_config`` 的兩個既有決定:

    * **symlink 先 resolve 再 replace**。把 dotfiles repo 裡的檔案 symlink 到
      ``~/.config/opencode/`` 是常見做法;直接 ``os.replace`` 到 symlink 本身會把
      連結換成普通檔,使用者的 dotfiles 從此不再同步 —— 而且沒有任何錯誤訊息。
    * **保留原檔權限**。使用者把它 chmod 成 600 是他的決定,同步不該擅自放寬。
    """
    backup = None
    mode = 0o644
    # `os.path.lexists` 不跟隨 symlink:失效的 symlink(指向還沒 clone 的 dotfiles)
    # 在 `Path.exists()` 下會回 False,於是這裡跳過 resolve、直接 `os.replace` 掉
    # 那個連結 —— 使用者的 dotfiles 從此不再同步,而且程式還宣稱「已安裝」。
    if os.path.lexists(target):
        # 失效的 symlink 在這裡 raise FileNotFoundError(OSError),由呼叫端處理:
        # `--sync-agents-md` 受控回 2,自動路徑只印 UNKNOWN 並保留連結。
        target = target.resolve(strict=True)
        mode = stat.S_IMODE(target.stat().st_mode)
        backup = _next_backup_path(target)
        shutil.copy2(target, backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.codetrail-", suffix=".tmp", dir=target.parent
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return backup


def _handle_agents_md(args: argparse.Namespace, config_path: Path) -> int:
    """檢查 / 安裝 / 同步全域 AGENTS.md。

    **回傳非零只有一種情況:使用者明確下了 ``--sync-agents-md`` 而寫入失敗。**
    漂移一律只是警告 —— ``aicode`` 對非零 rc 是硬退出,把「使用者自訂過
    AGENTS.md」變成開不了 OpenCode 是不能接受的。
    """
    if _truthy(os.environ.get(AGENTS_MD_SKIP_ENV)):
        return 0
    try:
        template = extract_agents_template(
            AGENTS_TEMPLATE_DOC.read_text(encoding="utf-8")
        )
    # UnicodeDecodeError 繼承 ValueError 而**不是** OSError:漏接的話,
    # 一個非 UTF-8 的檔案就會讓這支 preflight 拋例外回非零,aicode 隨即硬退出。
    except (OSError, UnicodeError, AgentsTemplateError) as exc:
        _print(f"UNKNOWN: 讀不到全域 AGENTS.md 範本({type(exc).__name__}: {exc});跳過該項檢查")
        return 0

    target = config_path.parent / AGENTS_MD_NAME
    try:
        # 同樣用 lexists:失效的 symlink 不是「沒有這份檔」,不得走自動安裝路徑。
        # 讀它會丟 FileNotFoundError,下面接住後印 UNKNOWN 並保留連結原狀。
        live = target.read_text(encoding="utf-8") if os.path.lexists(target) else None
    except (OSError, UnicodeError) as exc:  # 同上:非 UTF-8 的 live 檔不得阻斷啟動
        if args.sync_agents_md:
            # 使用者明確要求同步,讀不到就是同步失敗 —— 靜默回 0 等於「照做了」的
            # 假象。自動路徑(--fix / 純檢查)仍然只印 UNKNOWN 並回 0。
            _print(f"SYNC_FAILED: {target}: {type(exc).__name__}: {exc}")
            _print("             失效的 symlink 請先修好指向,或直接刪掉再跑一次。")
            return 2
        _print(f"UNKNOWN: 讀不到 {target}({type(exc).__name__});跳過該項檢查")
        return 0

    status, notes = agents_md_status(live, template)
    sync_cmd = "python3 scripts/opencode_contract_check.py --sync-agents-md"

    if args.sync_agents_md:
        if status == "ok":
            _print(f"SAFE: 全域 AGENTS.md 已與範本一致 ({target})")
            return 0
        try:
            backup = _write_agents_md(target, template)
        except OSError as exc:
            _print(f"SYNC_FAILED: {target}: {type(exc).__name__}: {exc}")
            return 2
        _print(f"SYNCED: 全域 AGENTS.md 已更新為範本內容 ({target})")
        if backup is not None:
            _print(f"        原檔備份: {backup}")
        _print("        改完要完全退出並重開 OpenCode、開新 session 才生效。")
        return 0

    if status == "ok":
        return 0
    if status == "missing":
        if not args.fix:
            _print(f"MISSING: 沒有全域 AGENTS.md ({target})")
            _print(f"         模型會少掉工具存在性與 RAG 觸發規則。執行: {sync_cmd}")
            return 0
        try:
            _write_agents_md(target, template)
        except OSError as exc:
            _print(f"INSTALL_FAILED: {target}: {type(exc).__name__}: {exc}")
            return 0
        _print(f"FIXED: 已安裝全域 AGENTS.md ({target})")
        _print("       改完要完全退出並重開 OpenCode、開新 session 才生效。")
        return 0

    label = "STALE" if status == "stale" else "INFO"
    _print(f"⚠ {label}: 全域 AGENTS.md 與範本不一致 ({target})")
    for note in notes:
        _print(f"         - {note}")
    if status == "stale":
        _print("         工具清單是模型的防幻覺錨點,對不上時模型會否認新工具存在。")
    _print(f"         同步(會備份原檔): {sync_cmd}")
    _print(f"         自訂過不想再被提醒: {AGENTS_MD_SKIP_ENV}=1")
    return 0


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
        "instructions entry (backup kept); also installs the global AGENTS.md "
        "when it is absent",
    )
    parser.add_argument(
        "--sync-agents-md",
        action="store_true",
        help="overwrite the global AGENTS.md with docs/opencode-agents-template.md "
        "(backup kept). Drift is only ever warned about otherwise, because it may "
        "be a deliberate customisation.",
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

    # 全域 AGENTS.md 與 opencode.json 同一個目錄、同一個「由 CodeTrail 管」的
    # 訊號。只有 --sync-agents-md 寫入失敗才會讓整支腳本非零(見函式 docstring)。
    agents_rc = _handle_agents_md(args, path)
    if agents_rc:
        return agents_rc

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
