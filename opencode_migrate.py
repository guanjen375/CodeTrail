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

所以遷移是「**還原** + 撤銷註冊」,不是「刪掉一堆鍵」。還原邏輯直接沿用
``compaction_mode`` 既有的 native 路徑(``apply_mode(mode="native")``):它的
ownership 語意(只還原現值仍等於 CodeTrail 寫入值的鍵)與那一整組安全測試都
還在,重寫一份等於把那些保證重新賭一次。

這裡只多做三件 native 路徑沒有的事:
  1. 一併移除**通知** plugin 項(native 只處理壓縮 plugin);
  2. 完成後**刪除**狀態檔,而不是寫一份 ``mode: native`` 的;
  3. 偵測自訂 instructions / 全域 AGENTS.md 並印出提示 —— **絕不自動搬**。

``mcp.codetrail`` 與 ``permission`` 一律不動:``mcp_server.py`` 仍然可以被任何
MCP client 用,拿掉等於替使用者決定他不能再這樣用。

沒有狀態檔、也沒有 CodeTrail plugin 項的機器:**一個 byte 都不動**。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import compaction_mode  # noqa: E402
import model_resolution  # noqa: E402

BACKUP_SUFFIX = ".codetrail-migrate.bak"

#: 這兩個檔名曾經被註冊進使用者的 opencode.json。
COMPACTION_PLUGIN = compaction_mode.PLUGIN_FILENAME
NOTIFY_PLUGIN = "codetrail-notify.js"
PLUGIN_DIR = REPO_ROOT / "opencode_plugins"

#: CodeTrail 曾經自己加進 instructions 的唯一一項。
OWN_INSTRUCTION = ".codetrail/lessons.md"


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
    _config: dict[str, Any] | None = None

    @property
    def needed(self) -> bool:
        return bool(self.changes or self.removed_plugins or self.state_present)

    def render(self) -> list[str]:
        lines: list[str] = []
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
    path, value, error = model_resolution.load_first_opencode_config(env)
    if error:
        raise MigrationError(f"讀不到 OpenCode 設定: {error}")
    if path is None or not isinstance(value, dict):
        return None, {}
    return path, value


def _plugin_entries(config: Mapping[str, Any], filename: str) -> list[str]:
    """只認**本 repo 路徑**的 plugin 項。

    同名但指向別處的一律不碰:那是別人的 plugin,不是我們註冊的。
    """
    raw = config.get(compaction_mode.PLUGIN_SECTION)
    if not isinstance(raw, list):
        return []
    target = str((PLUGIN_DIR / filename).resolve())
    found: list[str] = []
    for item in raw:
        text = compaction_mode.plugin_spec_text(item)
        if not text:
            continue
        local = compaction_mode.plugin_local_path(text)
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

    state_path = compaction_mode.state_path(environ)
    plan.state_path = state_path
    state = None
    try:
        state = compaction_mode.load_state(state_path, environ)
    except Exception as exc:  # noqa: BLE001
        if state_path.exists() or state_path.is_symlink():
            # 狀態檔**在**卻讀不了(壞掉、權限不對、symlink):這不是「沒接管過」。
            # 它記的正是還原用的原值;吞成 warning 等於讓那些舊值永遠沒人還原,
            # 而且 aicode 從此不再提示。fail-loud,讓使用者處理那個檔。
            raise MigrationError(
                f"ownership 狀態檔存在但無法信任({exc}):{state_path}。"
                "請修好或移除它(移除等於放棄自動還原,舊值要自己改回)之後再重跑。"
            ) from exc
        plan.warnings.append(f"ownership 狀態檔讀不到({exc});只會處理 plugin 項。")
    if state is None and (state_path.exists() or state_path.is_symlink()):
        # load_state 對壞掉 / 權限過寬 / symlink 的狀態檔是 fail-closed 回 None
        # (「沒有可信的接管紀錄」),但檔案**在**就不是「沒接管過」:見上面。
        raise MigrationError(
            f"ownership 狀態檔存在但無法信任:{state_path}。"
            "請修好或移除它(移除等於放棄自動還原,舊值要自己改回)之後再重跑。"
        )
    plan.state_present = state is not None

    config_path, config = _load_config(environ)
    plan.config_path = config_path
    if not config or config_path is None:
        return plan

    working = json.loads(json.dumps(config))  # 深拷貝,plan 階段不動原件
    plugin_path = PLUGIN_DIR / COMPACTION_PLUGIN
    changes, warnings, errors, _state = compaction_mode.apply_mode(
        working,
        mode=compaction_mode.MODE_NATIVE,
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
        entries = working.get(compaction_mode.PLUGIN_SECTION)
        if isinstance(entries, list):
            working[compaction_mode.PLUGIN_SECTION] = [
                item
                for item in entries
                if compaction_mode.plugin_spec_text(item) != spec
            ]
    entries = working.get(compaction_mode.PLUGIN_SECTION)
    if isinstance(entries, list) and not entries and not _plugin_existed_before(state):
        working.pop(compaction_mode.PLUGIN_SECTION, None)

    plan.notes = _custom_instruction_notes(working)
    plan._config = working
    return plan


def _plugin_existed_before(state: Mapping[str, Any] | None) -> bool:
    entry = (state or {}).get(compaction_mode.PLUGIN_SECTION)
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
        if not compaction_mode.clear_state(plan.state_path, environ):
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
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        plan = apply_migration(dry_run=args.check)
    except MigrationError as exc:
        print(f"[migrate] {exc}", file=sys.stderr)
        return 2
    for line in plan.render():
        print(f"[migrate] {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
