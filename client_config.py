#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_config — 客戶端的使用者設定 ``~/.config/codetrail/client.json``。

CodeTrail 的設定只有三個來源,**全部是檔案**:

1. `config.py` 的 repo 常數 —— 改它就是改 repo,所有使用者一致;
2. `~/.config/codetrail/{deployment,models}.json` —— 每台機器的 server、端點、
   模型、n_ctx(由 `set_config.sh` 產生);
3. **這個檔** —— 每個使用者的開關。

環境變數不是來源。殼層裡殘留的 `AICODE_*` / `AI_CODE_*` / `CODETRAIL_*`(不論
來自哪個 branch 的舊文件)對 runtime 一律無效:子行程的環境在交出去之前就被
剝掉,行程之間用 argv 交接。

**沒有這個檔 = 全部預設**,而且預設是 fail-closed 的那一邊(不匯入外部檔案、
不收集資料、不放行遠端端點、壓縮退成 ``manual``)。`git pull` 之後不會有任何
東西自己啟用。

**未知鍵 fail-loud**:寫錯鍵名靜默忽略,就是「我設了但沒生效」與「我設對了」
長得一模一樣。

檔案是 owner-only(目錄 0700 / 檔 0600、拒 symlink、原子替換):它決定寫入
工具要不要人工核准,能被別人改就等於能繞過核准。
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import client_compaction
import client_paths
import client_policy

SCHEMA = 1
CONFIG_PARTS = (".config", "codetrail", "client.json")

DIR_MODE = 0o700
FILE_MODE = 0o600
MAX_BYTES = 256 * 1024

PERMISSION_VALUES = ("allow", "ask", "deny")

#: 相容既有設定鍵,只接受拒絕降級的 error。
RERANK_FALLBACK_VALUES = ("error",)

#: `.h` 當成哪一種語言解析。
H_LANG_VALUES = ("c", "cpp")


class ClientConfigError(RuntimeError):
    """client.json 的位置、權限或內容不合契約。"""


def _error(message: str) -> ClientConfigError:
    return ClientConfigError(message)


@dataclass(frozen=True)
class ClientSettings:
    """一份 client.json。欄位順序即文件順序。

    每一個布林開關的預設都是「關」,而且理由都一樣:開著的那一邊會把資料送出
    這台機器、或讓模型碰到專案以外的東西。沒有設定檔的新安裝不該自動落在那一邊。
    """

    path: Path
    present: bool = False
    compaction_mode: str = client_compaction.DEFAULT_MODE
    permission: dict[str, str] = field(default_factory=dict)
    #: 主模型端點非 loopback 時,才放行送出 prompt。
    model_remote_ok: bool = False
    #: Exact per-role authorization. Deployment URLs themselves grant nothing.
    model_endpoints: dict[str, str] = field(default_factory=dict)
    #: KB chunk 脈絡生成非 loopback 時,才放行送出**整份文件的窗**。
    #: 與上面那個是**兩個**鍵:資料範圍不同(prompt vs 整份文件),合併會把前者的
    #: 同意無聲擴大成後者。
    kb_context_remote_ok: bool = False
    #: 允許 `import_external_file` 從專案外複製檔案進來,以及允許的來源根目錄。
    #: 開了之後**每一次**匯入仍然要人工核准(見 `client_policy.ASK_TOOLS`):
    #: 這個開關把授權從「單次啟動」變成「跨專案持久」,所以實際動作要逐次確認。
    external_import: bool = False
    external_import_roots: list[str] = field(default_factory=list)
    #: 把 make / cmake / ninja / meson / bazel 掛進 run_command 白名單。
    #: 它們會跑專案內的 build script = 任意程式碼執行,所以只在分析自己的專案時開。
    build_commands: bool = False
    #: reranker 掛掉時的行為。
    rerank_fallback_policy: str = "error"
    #: 讀被分析專案的 `AGENTS.md` 與 `.codetrail/lessons.md`。分析不信任的 repo
    #: 時關掉它 —— 那兩份檔每一輪都會進 system prompt。
    project_instructions: bool = True
    #: 反組譯用的 objdump(跨架構韌體需要 binutils-<triplet>)。空字串 = 用 PATH 上的。
    objdump: str = ""
    #: `.h` 當成 c 還是 cpp 解析。
    h_lang: str = "c"
    #: 用容器跑 run_command。
    use_container: bool = False
    #: 畫面上顯示模型的 thinking。**只管畫面**——它是 `/thinking` 的初始值。
    show_reasoning: bool = False
    #: 舊回合的 reasoning 要不要送進模型。**只管送模 payload 與摘要輸入**。
    #: 與上面那個是**兩個**鍵:合併之後純 UI 操作會改變模型看到的 context,
    #: 而且「顯示但不送」與「送但不顯示」這兩種組合至少會有一種變成不可能。
    keep_historical_reasoning: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "compaction_mode": self.compaction_mode,
            "permission": dict(self.permission),
            "model_remote_ok": self.model_remote_ok,
            "model_endpoints": dict(self.model_endpoints),
            "kb_context_remote_ok": self.kb_context_remote_ok,
            "external_import": self.external_import,
            "external_import_roots": list(self.external_import_roots),
            "build_commands": self.build_commands,
            "rerank_fallback_policy": self.rerank_fallback_policy,
            "project_instructions": self.project_instructions,
            "objdump": self.objdump,
            "h_lang": self.h_lang,
            "use_container": self.use_container,
            "show_reasoning": self.show_reasoning,
            "keep_historical_reasoning": self.keep_historical_reasoning,
        }

    def with_compaction(self, mode: str) -> "ClientSettings":
        """只換壓縮模式,其餘每一個鍵原封不動。

        `set_config` 重跑時用這個。以前它是重新 `ClientSettings(...)` 只帶
        `compaction_mode` 與 `permission`,於是使用者手動加的每一個鍵都被序列化
        回預設值 —— `project_instructions: false` 會靜默變回 `true`,下一次啟動
        就開始讀不信任 repo 的指示,而且沒有任何訊息。
        """
        return replace(self, compaction_mode=mode)

    def banner(self) -> str:
        if not self.present:
            return (
                f"壓縮模式 manual(沒有 {self.path});沒有設定檔就等於沒有接管 —— "
                "要自動壓縮請跑 ./set_config.sh。"
            )
        return f"壓縮模式 {self.compaction_mode}(來自 {self.path})"


def config_path(env: Mapping[str, str] | None = None) -> Path:
    """`~/.config/codetrail/client.json`。只由 HOME 推導,沒有覆寫變數。

    以前這裡認 `CODETRAIL_CLIENT_CONFIG`。刪掉的理由:這個檔決定**互動 session
    的工具權限**(`permission` 覆寫),殼層裡一個變數就能把 `apply_patch` 從
    ask 翻成 allow —— 使用者以為每一次寫檔都會問,實際上不會。要讓某個內部
    入口(session_eval 的 replay)用另一份設定,走的是那個子行程的隱藏 argv
    旗標 `run --client-config`,呼叫端明確指定,不是環境。
    """
    home = (env or os.environ).get("HOME") or str(Path.home())
    return Path(home).joinpath(*CONFIG_PARTS)


#: 布林開關 → ClientSettings 的欄位名。順序即文件順序。
_BOOL_KEYS = (
    "model_remote_ok",
    "kb_context_remote_ok",
    "external_import",
    "build_commands",
    "project_instructions",
    "use_container",
    "show_reasoning",
    "keep_historical_reasoning",
)
#: 有限字串集合的鍵 → 允許值。
_CHOICE_KEYS = {
    "rerank_fallback_policy": RERANK_FALLBACK_VALUES,
    "h_lang": H_LANG_VALUES,
}
#: 自由字串的鍵。
_TEXT_KEYS = ("objdump",)
#: 全部合法鍵。**未知鍵 fail-loud** —— 寫錯鍵名靜默忽略,就是「我設了但沒生效」
#: 與「我設對了」長得一模一樣。
KNOWN_KEYS = frozenset(
    {"schema", "compaction_mode", "permission", "external_import_roots", "model_endpoints"}
    | set(_BOOL_KEYS)
    | set(_CHOICE_KEYS)
    | set(_TEXT_KEYS)
)
#: 已移除的鍵 → 給使用者的說明。留在檔裡一律 fail-loud,不能只當未知鍵略過:
#: `"collect_data": false` 被靜默忽略等於「我關了但它還在收」,與拼錯鍵是同一種無聲失敗。
REMOVED_KEYS: dict[str, str] = {
    "collect_data": (
        "資料收集已改成永久開啟(只有 readonly 評測 session 不寫),沒有開關,請把這個鍵拿掉;"
        "檔案在 ~/.local/state/codetrail/data/<root 雜湊>/interactions.jsonl,"
        "`python3 data_flywheel.py where --root <專案>` 會印出確切目錄"
    ),
}


def _bool(value: Any, key: str, path: Path) -> bool:
    """只認真的 JSON boolean。

    ``"false"`` 是一個非空字串,``bool("false")`` 是 True —— 接受字串等於讓
    「我明明寫了 false」變成「開著」。
    """
    if not isinstance(value, bool):
        raise ClientConfigError(f"{path} 的 {key} 必須是 true / false,得到 {value!r}")
    return value


def _validate(value: Any, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ClientConfigError(f"{path} 的根節點必須是 JSON object")
    schema = value.get("schema")
    # `isinstance(True, int)` 與 `True == 1` 都成立,所以 `"schema": true` 會穿過
    # 一個單純的 `!= SCHEMA` 比對。型別先驗,再比值。
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != SCHEMA:
        raise ClientConfigError(f"{path} 的 schema 必須是 {SCHEMA},得到 {schema!r}")
    removed = sorted(set(value) & set(REMOVED_KEYS))
    if removed:
        raise ClientConfigError(
            f"{path} 有已移除的鍵 {removed}:" + ";".join(REMOVED_KEYS[key] for key in removed)
        )
    unknown = sorted(set(value) - KNOWN_KEYS)
    if unknown:
        raise ClientConfigError(
            f"{path} 有不認得的鍵 {unknown};合法鍵:{sorted(KNOWN_KEYS)}。"
            "拼錯的鍵靜默忽略會讓「設了沒生效」與「設對了」長得一樣。"
        )
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

    fields: dict[str, Any] = {"compaction_mode": mode, "permission": permission}
    if "model_endpoints" in value:
        from endpoint_policy import validate_model_endpoints, EndpointPolicyError
        try:
            fields["model_endpoints"] = validate_model_endpoints(value["model_endpoints"])
        except EndpointPolicyError as exc:
            raise ClientConfigError(f"{path}: {exc}") from exc
    defaults = ClientSettings(path=path)
    for key in _BOOL_KEYS:
        if key in value:
            fields[key] = _bool(value[key], key, path)
    for key, allowed in _CHOICE_KEYS.items():
        if key in value:
            raw = value[key]
            if raw not in allowed:
                if key == "rerank_fallback_policy":
                    raise ClientConfigError(
                        f'{path} 的 rerank_fallback_policy 只接受 "error";'
                        '請移除舊值或改成 "error",並修復專用 reranker。'
                    )
                raise ClientConfigError(
                    f"{path} 的 {key} 必須是 {list(allowed)} 之一,得到 {raw!r}"
                )
            fields[key] = raw
    for key in _TEXT_KEYS:
        if key in value:
            raw = value[key]
            if not isinstance(raw, str):
                raise ClientConfigError(f"{path} 的 {key} 必須是字串,得到 {raw!r}")
            fields[key] = raw
    roots = value.get("external_import_roots", getattr(defaults, "external_import_roots"))
    # `not item` 只擋得掉 ""。`"   "` 會通過這裡、在下面被 strip 成空,於是整份
    # 白名單變成空陣列 —— 而空陣列在 `external_import` 那邊等於「用預設來源」。
    # 使用者以為自己縮小了範圍,實際上是回到 ~/Downloads 與 /tmp。
    if not isinstance(roots, list) or any(
        not isinstance(item, str) or not item.strip() for item in roots
    ):
        raise ClientConfigError(f"{path} 的 external_import_roots 必須是非空字串的陣列")
    if fields.get("external_import") and not roots:
        # 開了匯入卻沒有來源白名單 = 整台機器都可以讀。這不是「預設全開」該有的
        # 便利,是使用者少寫了一半設定。
        raise ClientConfigError(
            f"{path} 開了 external_import 但 external_import_roots 是空的;"
            "請明列允許的來源根目錄(例如 [\"~/Downloads\"])。"
        )
    fields["external_import_roots"] = [item.strip() for item in roots if item.strip()]
    return fields


def load_client_settings_from(path: Path) -> ClientSettings:
    """讀指定位置的 client.json(內部入口的隱藏旗標用)。

    仍走同一套 owner-only 防線與同一個 validator:replay 用自己寫的設定,不代表
    那份設定可以是 0644、可以是 symlink、或可以帶未知鍵。
    """
    raw = client_paths.read_private_file(
        path.parent, path.name, _error, max_bytes=MAX_BYTES, anchor=path.parent.parent
    )
    if raw is None:
        raise ClientConfigError(f"找不到指定的 client.json:{path}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientConfigError(f"{path} 不是合法 JSON: {exc}") from exc
    return ClientSettings(path=path, present=True, **_validate(value, path))


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
    return ClientSettings(path=path, present=True, **_validate(value, path))


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


def apply_to_config(settings: ClientSettings, *, readonly: bool = False) -> None:
    """把使用者開關推進 runtime。**這是唯一一個入口。**

    為什麼是 mutate 而不是「各模組自己讀 client.json」:各自讀等於各自有一份
    快照,而 `readonly` 這種必須壓過使用者偏好的決定就沒有單一施力點。
    §3 的規則:動態值只用 ``import config``,不用 ``from config import X``。

    ``readonly=True``(評測 / 抽查 / canary)之後才套用,而且**壓過**上面所有值:
    它是邊界,不是可以被 client.json 調和的偏好。
    """
    import config

    if settings.rerank_fallback_policy != "error":
        raise ClientConfigError('rerank_fallback_policy 只接受 "error";請修復專用 reranker')
    config.EXTERNAL_IMPORT_ENABLED = settings.external_import
    config.EXTERNAL_IMPORT_ROOTS = list(settings.external_import_roots)
    config.KB_CONTEXT_REMOTE_OK = settings.kb_context_remote_ok
    config.MODEL_REMOTE_OK = settings.model_remote_ok
    config.MODEL_ENDPOINTS = dict(settings.model_endpoints)
    config.RERANK_FALLBACK_POLICY = settings.rerank_fallback_policy
    config.PROJECT_INSTRUCTIONS_ENABLED = settings.project_instructions
    config.OBJDUMP = settings.objdump
    config.H_LANG = settings.h_lang
    config.USE_CONTAINER = settings.use_container

    if readonly:
        # replay / canary 的契約是「前後 project state 不變」,而且它們不得把
        # 任何東西送出這台機器。這幾條一律關到底,client.json 翻不回來。
        config.EXTERNAL_IMPORT_ENABLED = False
        config.EXTERNAL_IMPORT_ROOTS = []
        # data flywheel 永久開啟、沒有使用者開關,所以**這裡是唯一**擋住
        # canary / eval / replay 把合成題目寫進使用者資料的地方。
        config.COLLECT_DATA = False
        config.USE_CONTAINER = False
        config.CTX_METRICS_ENABLED = False


def policy_for(settings: ClientSettings, base: client_policy.PermissionPolicy | None = None):
    """把使用者的權限覆寫套在 base policy 上。"""
    base_policy = base or client_policy.InteractivePolicy()
    if not settings.permission:
        return base_policy
    return client_policy.OverridePolicy(base_policy, settings.permission)
