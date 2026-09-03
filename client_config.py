#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_config — 客戶端的使用者設定 ``~/.config/codetrail/client.json``。

只放兩件**使用者顯式選過**的事:壓縮模式與權限覆寫。其餘(模型、n_ctx、
端點)仍由 deployment profile 與 env 決定,不在這裡再開一個會漂移的來源。

**沒有這個檔 = 沒有接管**:壓縮模式退成 ``manual``,啟動橫幅會講明。這條
fail-closed 預設沿用舊的 ownership 狀態檔語意 —— `git pull` 之後不會有任何
東西自己啟用。

檔案是 owner-only(目錄 0700 / 檔 0600、拒 symlink、原子替換):它決定寫入
工具要不要人工核准,能被別人改就等於能繞過核准。
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import client_compaction
import client_paths
import client_policy

SCHEMA = 1
CONFIG_PARTS = (".config", "codetrail", "client.json")
CONFIG_PATH_ENV = "CODETRAIL_CLIENT_CONFIG"

DIR_MODE = 0o700
FILE_MODE = 0o600
MAX_BYTES = 256 * 1024

PERMISSION_VALUES = ("allow", "ask", "deny")


class ClientConfigError(RuntimeError):
    """client.json 的位置、權限或內容不合契約。"""


def _error(message: str) -> ClientConfigError:
    return ClientConfigError(message)


@dataclass(frozen=True)
class ClientSettings:
    path: Path
    present: bool = False
    compaction_mode: str = client_compaction.DEFAULT_MODE
    permission: dict[str, str] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "compaction_mode": self.compaction_mode,
            "permission": dict(self.permission),
        }

    def banner(self) -> str:
        if not self.present:
            return (
                f"壓縮模式 manual(沒有 {self.path});沒有設定檔就等於沒有接管 —— "
                "要自動壓縮請跑 ./set_config.sh。"
            )
        return f"壓縮模式 {self.compaction_mode}(來自 {self.path})"


def config_path(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    override = str(environ.get(CONFIG_PATH_ENV, "")).strip()
    if override:
        return Path(override).expanduser()
    home = environ.get("HOME") or str(Path.home())
    return Path(home).joinpath(*CONFIG_PARTS)


def _validate(value: Any, path: Path) -> tuple[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise ClientConfigError(f"{path} 的根節點必須是 JSON object")
    if value.get("schema") != SCHEMA:
        raise ClientConfigError(f"{path} 的 schema 必須是 {SCHEMA},得到 {value.get('schema')!r}")
    mode = value.get("compaction_mode")
    if mode not in client_compaction.MODES:
        raise ClientConfigError(
            f"{path} 的 compaction_mode 必須是 {list(client_compaction.MODES)} 之一,得到 {mode!r}"
        )
    raw_permission = value.get("permission", {})
    if not isinstance(raw_permission, dict):
        raise ClientConfigError(f"{path} 的 permission 必須是 object")
    permission: dict[str, str] = {}
    for tool, decision in raw_permission.items():
        if not isinstance(tool, str) or not tool:
            raise ClientConfigError(f"{path} 的 permission 鍵必須是工具名")
        if decision not in PERMISSION_VALUES:
            raise ClientConfigError(
                f"{path} 的 permission[{tool}] 必須是 {list(PERMISSION_VALUES)} 之一,得到 {decision!r}"
            )
        permission[tool] = decision
    return mode, permission


def load_client_settings(env: Mapping[str, str] | None = None) -> ClientSettings:
    """讀設定。**檔案不存在不是錯誤** —— 回 fail-closed 的預設。

    存在但壞掉 / 權限過寬則 fail-loud:靜默退回預設等於使用者以為自己設過的
    自動壓縮與權限覆寫都還在,實際上都沒了。
    """
    path = config_path(env)
    try:
        raw = client_paths.read_private_file(
            path.parent, path.name, _error, max_bytes=MAX_BYTES, anchor=path.parent.parent
        )
    except ClientConfigError:
        raise
    except OSError as exc:  # pragma: no cover - 由 read_private_file 轉成 ClientConfigError
        raise ClientConfigError(f"無法讀取 {path}: {exc}") from exc
    if raw is None:
        return ClientSettings(path=path)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientConfigError(f"{path} 不是合法 JSON: {exc}") from exc
    mode, permission = _validate(value, path)
    return ClientSettings(path=path, present=True, compaction_mode=mode, permission=permission)


def save_client_settings(
    settings: ClientSettings, env: Mapping[str, str] | None = None
) -> Path:
    """owner-only、原子替換,全程錨在 dir fd 上。

    父目錄與檔案都不跟 symlink:只驗最終檔案擋不住「把 ``codetrail`` 目錄
    換成 symlink」,而這個檔決定寫入工具要不要人工核准。
    """
    path = config_path(env)
    payload = (
        json.dumps(settings.as_json(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    # anchor 在 `~/.config`:`codetrail/` 那一層是我們的,不跟 symlink。
    return client_paths.replace_private_file(
        path.parent, path.name, payload, _error, anchor=path.parent.parent
    )


def policy_for(settings: ClientSettings, base: client_policy.PermissionPolicy | None = None):
    """把使用者的權限覆寫套在 base policy 上。"""
    base_policy = base or client_policy.InteractivePolicy()
    if not settings.permission:
        return base_policy
    return client_policy.OverridePolicy(base_policy, settings.permission)
