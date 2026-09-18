#!/usr/bin/env python3
"""Verify CodeTrail MCP wiring and the active model's real tool-call path.

``aicode`` runs this before starting the CodeTrail client.  The protocol check
is always live: it starts ``mcp_server.py`` through the same
``client_mcp.McpClient`` the client itself uses, performs initialize/tools/list,
verifies the exact public tool contract, and calls the read-only ``list_dir``
tool.  A named explicit model probe is a hard gate and may retry once; a
separate unnamed-intent probe runs once and records an
optimal/suboptimal/fail/timeout diagnostic without blocking startup.

The model probes run the real headless client (``codetrail_chat.py run
--format json``) under the **read-only** permission policy, so a canary can
never write to the project, and **ephemeral** session storage, so it never
leaves a conversation in the user's session list.

The model checks are comparatively slow, so results are cached in separate
explicit/implicit lanes by a configuration/runtime fingerprint.  Each record
contains only fingerprint, status, time, and canary version -- never prompts,
model/tool output, project paths, session ids, or config contents.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import client_events  # noqa: E402
import process_env  # noqa: E402
import client_mcp  # noqa: E402
import client_prompt  # noqa: E402
import config as codetrail_config  # noqa: E402
from mcp_contract import MCP_INSTRUCTIONS, PUBLIC_TOOL_NAMES, PUBLIC_TOOL_ORDER  # noqa: E402
from model_resolution import resolve_main_model  # noqa: E402

# 3:換成自帶的客戶端之後,指紋輸入整組改變(不再有舊世代前端的設定 / 版本 /
# build prompt;改為客戶端版本與 system prompt digest)。舊快取項不得沿用。
CANARY_VERSION = 3
CACHE_SCHEMA = 3
#: canary 要驗的**就是 wrapper 等一下會 exec 的那一個**客戶端 —— 也就是這個
#: repo 裡的 `codetrail_chat.py`。以前這裡有一個 `AICODE_CLIENT_ENTRY` 覆寫,
#: 目的是「wrapper 換客戶端時 canary 也跟著換」;但 wrapper 已經只 exec 自己
#: 旁邊那一份,那個覆寫留著只剩一個效果:殼層設一個值,canary 就去驗另一份
#: 程式,對一個 policy / system prompt / 事件契約完全不同的客戶端回報 PASS。
CLIENT_ENTRY = REPO_ROOT / "codetrail_chat.py"


def client_entry() -> Path:
    return CLIENT_ENTRY
MODEL_CANARY_HEARTBEAT_SECONDS = 15

# 時限與快取期是 repo 常數(config.py),不是環境變數 —— 見那邊的說明。
TOOL_CANARY_MCP_TIMEOUT_SECONDS = codetrail_config.TOOL_CANARY_MCP_TIMEOUT_SECONDS
TOOL_CANARY_MODEL_TIMEOUT_SECONDS = codetrail_config.TOOL_CANARY_MODEL_TIMEOUT_SECONDS
TOOL_CANARY_IMPLICIT_TIMEOUT_SECONDS = (
    codetrail_config.TOOL_CANARY_IMPLICIT_TIMEOUT_SECONDS
)
TOOL_CANARY_TTL_SECONDS = codetrail_config.TOOL_CANARY_TTL_SECONDS


def default_base_url() -> str:
    """主 llama-server URL:deployment profile 是唯一來源。

    只交 HOME(定位 `~/.config/codetrail/deployment.json`)。那個模組拿 environ
    只為了找檔案,設定值一律來自檔案 —— canary 探測的必須就是客戶端等一下會用的
    那一台 server。
    """
    import deployment_profile

    home = os.environ.get("HOME")
    if home:
        keep = {"HOME": home}
    else:
        # Windows fallback,而且**只有** HOME 缺席時才交。
        profile = os.environ.get("USERPROFILE")
        keep = {"USERPROFILE": profile} if profile else {}
    try:
        return deployment_profile.load_effective_profile(keep).service("main").base_url
    except deployment_profile.ProfileError as exc:
        raise CanaryError(f"deployment profile 無法載入:{exc}") from exc
MAX_CACHE_ENTRIES = 32
MAX_PROPS_BYTES = 4 * 1024 * 1024

EXPECTED_MCP_TOOLS = PUBLIC_TOOL_NAMES
EXPECTED_MCP_TOOL_ORDER = PUBLIC_TOOL_ORDER
TARGET_FRONTEND_TOOL = "list_dir"
CANARY_PROMPT = (
    "請立即呼叫 list_dir，path=\".\"、depth=1。"
    "必須實際呼叫工具，不准用文字回答工具是否存在。"
)
IMPLICIT_CANARY_PROMPT = (
    "請實際檢查目前專案根目錄有哪些檔案與子目錄，然後只用一句話確認已完成檢查。"
    "不要猜測，也不要只描述你打算怎麼做。"
)

# A completed call to one of these tools proves autonomous CodeTrail routing,
# but is not the optimal response to the directory-listing intent above.
IMPLICIT_READ_ONLY_FRONTEND_TOOLS = frozenset(
    {
        "list_dir",
        "read_file",
        "grep_code",
        "code_rag_search",
        "file_info",
        "query_knowledge",
        "query_knowledge_strict",
        "git_status",
        "git_diff",
        "analyze_file",
    }
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


class CanaryError(RuntimeError):
    """An actionable health-check failure whose message is safe to print."""


@dataclass(frozen=True)
class ModelEvidence:
    success: bool
    reason: str
    session_ids: tuple[str, ...] = ()
    saw_tool_calls_finish: bool = False
    failure_kind: str = ""


class ImplicitStatus(str, Enum):
    OPTIMAL = "optimal"
    SUBOPTIMAL = "suboptimal"
    FAIL = "fail"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class ProtocolEvidence:
    tools_digest: str
    instructions_digest: str


@dataclass(frozen=True)
class ImplicitEvidence:
    status: ImplicitStatus
    session_ids: tuple[str, ...] = ()


def _print(message: str, *, error: bool = False) -> None:
    print(f"[tool-health] {message}", file=sys.stderr if error else sys.stdout, flush=True)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def _run_process(
    argv: Sequence[str],
    *,
    root: Path,
    timeout: int,
) -> process_env.CompletedProcess[str]:
    return process_env.run(
        list(argv),
        cwd=str(root),
        stdin=process_env.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _run_process_with_heartbeat(
    argv: Sequence[str],
    *,
    root: Path,
    timeout: int,
    heartbeat: float = MODEL_CANARY_HEARTBEAT_SECONDS,
) -> process_env.CompletedProcess[str]:
    """``_run_process`` with periodic progress lines while the child runs.

    The live model canary regularly takes tens of seconds on local hardware;
    with zero output users assume ``aicode`` is hung.  Timeout behaviour
    matches ``_run_process``: raise ``process_env.TimeoutExpired`` carrying any
    partial output collected so far.
    """
    with process_env.popen(
        list(argv),
        cwd=str(root),
        stdin=process_env.DEVNULL,
        stdout=process_env.PIPE,
        stderr=process_env.PIPE,
        text=True,
    ) as process:
        started = time.monotonic()
        try:
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    process.kill()
                    stdout, stderr = process.communicate()
                    raise process_env.TimeoutExpired(
                        list(argv), timeout, output=stdout, stderr=stderr
                    )
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(heartbeat, remaining)
                    )
                except process_env.TimeoutExpired:
                    elapsed = int(time.monotonic() - started)
                    _print(
                        f"headless run 仍在執行… 已 {elapsed} 秒"
                        f"（單次上限 {timeout} 秒）"
                    )
                    continue
                return process_env.CompletedProcess(
                    list(argv), process.returncode, stdout, stderr
                )
        except BaseException:
            process.kill()
            raise


def run_protocol_roundtrip(
    *, root: Path, timeout: int
) -> tuple[tuple[str, ...], bool, ProtocolEvidence]:
    """initialize → tools/list → list_dir,走客戶端真正用的那一條路。

    「實際會執行什麼」就是 `client_mcp` 裡那一行 —— 再去讀一份設定只會製造
    另一個會漂移的來源。

    **不傳 env**:`McpClient` 的 `env=` 是**覆寫**通道(呼叫端明確要求的值),
    它在剝除之後才套用。把整份 `os.environ` 從那裡灌進去,等於把
    `process_env.STRIPPED_ENV_PREFIXES` 剛剝掉的那幾組原封不動加回去 ——
    包含核准後的 `run_command` 子行程會繼承的那些機密。client 本來就會繼承
    這個行程的環境,不需要我們再遞一次。
    """
    client = client_mcp.McpClient(root, start_timeout=float(timeout))
    try:
        try:
            specs = client.tools()
        except client_mcp.McpClientError as exc:
            raise CanaryError(f"MCP initialize/tools/list 失敗:{exc}") from exc
        names = tuple(spec.name for spec in specs)
        canonical = [
            {
                "name": spec.name,
                "description": spec.description,
                "inputSchema": spec.input_schema,
                "readOnlyHint": spec.read_only,
            }
            for spec in specs
        ]
        evidence = ProtocolEvidence(
            tools_digest=_json_digest(canonical),
            # Match scripts.mcp_catalog.text_digest exactly: MCP instructions
            # identity is raw UTF-8, not JSON-quoted text.
            instructions_digest=hashlib.sha256(
                MCP_INSTRUCTIONS.encode("utf-8")
            ).hexdigest(),
        )
        try:
            result = client.call("list_dir", {"path": ".", "depth": 1})
        except client_mcp.McpClientError as exc:
            raise CanaryError(f"MCP list_dir round-trip 失敗:{exc}") from exc
        return names, bool(result.is_error), evidence
    finally:
        client.close()


def run_protocol_check(*, root: Path, timeout: int) -> ProtocolEvidence:
    try:
        names, list_dir_error, evidence = run_protocol_roundtrip(
            root=root, timeout=timeout
        )
    except CanaryError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CanaryError(
            "MCP initialize/tools/list/list_dir 失敗"
            f"({type(exc).__name__};server 詳細 log 已避免輸出)"
        ) from exc

    name_set = frozenset(names)
    missing = sorted(EXPECTED_MCP_TOOLS - name_set)
    unexpected = sorted(name_set - EXPECTED_MCP_TOOLS)
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"缺少={missing}")
        if unexpected:
            details.append(f"未同步新增={unexpected}")
        raise CanaryError(
            f"MCP 工具 contract 不符:預期 {len(EXPECTED_MCP_TOOLS)}、實得 {len(names)};"
            + ";".join(details)
        )
    if names != EXPECTED_MCP_TOOL_ORDER:
        raise CanaryError(
            "MCP 工具順序不符 canonical PUBLIC_TOOL_ORDER;"
            "client catalog 與 routing fingerprint 不可靜默漂移"
        )
    if list_dir_error:
        raise CanaryError("MCP list_dir round-trip 回傳 isError=true")
    return evidence


def _json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_digest(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"


def _server_root_url(base_url: str) -> str:
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return root


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """loopback /props 探測不跟 3xx:回 None → HTTPError,呼叫端當 unreachable。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _local_probe_opener() -> urllib.request.OpenerDirector:
    """proxy 衛生:不讀環境 HTTP(S)_PROXY、不跟 redirect 的 opener。"""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirectHandler()
    )


def fetch_main_server_props(
    base_url: str,
    *,
    timeout: int = 5,
) -> dict[str, Any] | None:
    """主 server 的 `/props`。`base_url` 由呼叫端從 deployment profile 給,
    不從環境變數猜 —— 殼層殘留一個指向別台機器的 URL,等於拿別人的
    chat_template / n_ctx 當自己的指紋輸入。"""
    url = _server_root_url(base_url.strip()) + "/props"
    import endpoint_policy
    endpoint_policy.ensure_allowed(url, "main")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with _local_probe_opener().open(request, timeout=timeout) as response:
            payload = response.read(MAX_PROPS_BYTES + 1)
    except (OSError, urllib.error.URLError, TimeoutError):
        return None
    if len(payload) > MAX_PROPS_BYTES:
        return None
    try:
        props = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return props if isinstance(props, dict) else None


def _model_file_signature(props: Mapping[str, Any]) -> dict[str, Any]:
    import config
    if config.DEPLOYMENT_MODE == "client":
        from model_identity import capture_model_identity
        return capture_model_identity("main", props=dict(props))
    model_path = props.get("model_path")
    if not isinstance(model_path, str) or not model_path:
        return {"path": "unknown"}
    signature: dict[str, Any] = {"path_hash": hashlib.sha256(model_path.encode()).hexdigest()}
    try:
        info = Path(model_path).stat()
    except OSError:
        signature["stat"] = "unavailable"
    else:
        signature["size"] = info.st_size
        signature["mtime_ns"] = info.st_mtime_ns
    return signature


def _client_prompt_digest(root: Path, env: Mapping[str, str]) -> str:
    """這一輪模型真的會看到的 system prompt 的身分。

    以前這一格是舊世代前端的 build prompt 與全域 AGENTS.md;現在 system prompt
    由客戶端自組(內建基底規則 + MCP 路由圖 + 專案 AGENTS.md + lessons +
    使用者全域指示)。任何一段變了,模型的路由行為就可能不同,舊的 canary
    判定不得沿用。
    """
    try:
        return client_prompt.build_system_prompt(root, env=env).digest
    except Exception:  # noqa: BLE001 - 指紋壞掉只該讓快取失效,不該擋啟動
        return "unavailable"


def _compaction_mode_digest(env: Mapping[str, str]) -> str:
    """壓縮模式的身分。模式換了,對話被截斷的時機就換了。"""
    try:
        import client_config

        settings = client_config.load_client_settings(env)
        return _json_digest(
            {
                "present": settings.present,
                "mode": settings.compaction_mode,
                "permission": settings.permission,
            }
        )
    except Exception:  # noqa: BLE001
        return "unavailable"


def build_fingerprint(
    *,
    root: Path,
    selected_model: str,
    props: Mapping[str, Any],
    env: Mapping[str, str],
    protocol_evidence: ProtocolEvidence | None = None,
) -> str:
    home_raw = (env.get("HOME") or env.get("USERPROFILE") or "").strip()
    home = Path(home_raw).expanduser() if home_raw else None
    tracked_files = {
        "canary": _file_digest(Path(__file__)),
        "mcp_server": _file_digest(REPO_ROOT / "mcp_server.py"),
        # 客戶端本身是路由行為的一部分:engine 的訊息組裝、工具 schema 轉換與
        # 權限 policy 改了,模型看到的東西就不同,舊判定不得沿用。
        "client_engine": _file_digest(REPO_ROOT / "client_engine.py"),
        "client_prompt": _file_digest(REPO_ROOT / "client_prompt.py"),
        "client_chat": _file_digest(client_entry()),
        # override 的路徑本身也要進指紋:同名但指到別的 checkout 時,
        # 只 hash 內容也可能剛好相同(例如兩份都還沒改)。
        "client_entry_path": str(client_entry()),
        "project_agents": _file_digest(root / "AGENTS.md"),
        # lessons 注入檔也是模型看到的「專案規則」:內容變了要讓 model canary
        # 快取失效(aicode 會在 canary 之前先 render 好這個檔)。
        "project_lessons": _file_digest(root / ".codetrail" / "lessons.md"),
    }
    if home is not None:
        tracked_files["user_instructions"] = _file_digest(
            client_prompt.user_instructions_path(env)
        )
        tracked_files["deployment"] = _file_digest(
            home / ".config" / "codetrail" / "deployment.json"
        )

    props_subset = {
        key: props.get(key)
        for key in (
            "model_alias",
            "model_path",
            "chat_template",
            "n_ctx",
            "n_batch",
            "n_ubatch",
            "n_parallel",
            "default_generation_settings",
        )
        if key in props
    }
    # These two objects are deliberately included in full.  New llama.cpp
    # capability/build fields must invalidate the cache without a canary code
    # change selecting them one by one.
    props_subset["chat_template_caps"] = props.get(
        "chat_template_caps", {"state": "missing"}
    )
    props_subset["build_info"] = props.get("build_info", {"state": "missing"})
    protocol = protocol_evidence or ProtocolEvidence(
        tools_digest="unavailable",
        instructions_digest="unavailable",
    )
    payload = {
        "canary_version": CANARY_VERSION,
        "root": str(root),
        "selected_model": selected_model,
        # 模型真的會看到的 system prompt 的身分(取代舊的 build prompt /
        # 全域 AGENTS.md 兩格)。
        "system_prompt_digest": _client_prompt_digest(root, env),
        "compaction_mode_digest": _compaction_mode_digest(env),
        "live_tools_digest": protocol.tools_digest,
        "mcp_instructions_digest": protocol.instructions_digest,
        "props": props_subset,
        "model_file": _model_file_signature(props),
        "files": tracked_files,
    }
    return _json_digest(payload)


#: 快取檔名帶 schema 號。
#:
#: 為什麼:同一台機器上可能同時裝著兩個世代的 CodeTrail(舊世代前端那一份是
#: ``CACHE_SCHEMA=2``、這一份是 3),而它們共用 ``~/.cache/codetrail``。讀到別的
#: schema 會被當成空快取再**整檔覆寫**,於是兩邊每次啟動都互相清空對方的紀錄,
#: 每一次 aicode 都要重跑一次幾十秒的 live canary。檔名分開之後兩份各記各的。
CACHE_FILENAME = f"tool-call-canary.v{CACHE_SCHEMA}.json"


def resolve_cache_path(env: Mapping[str, str]) -> Path | None:
    """快取檔位置。只由 `XDG_CACHE_HOME` / `HOME` 推導 —— 沒有覆寫變數。

    要強制重測 = 刪掉那個檔(或 `--force`),不是設一個要查文件才知道的變數。
    """
    xdg = (env.get("XDG_CACHE_HOME") or "").strip()
    if xdg:
        return Path(xdg).expanduser() / "codetrail" / CACHE_FILENAME
    home = (env.get("HOME") or env.get("USERPROFILE") or "").strip()
    if not home:
        return None
    return Path(home).expanduser() / ".cache" / "codetrail" / CACHE_FILENAME


_CACHE_LANES = ("explicit", "implicit")
_CACHE_ENTRY_KEYS = frozenset(
    {"fingerprint", "status", "checked_at", "canary_version"}
)
_CACHE_STATUSES = {
    "explicit": frozenset({"pass"}),
    "implicit": frozenset(status.value for status in ImplicitStatus),
}


def _empty_cache() -> dict[str, Any]:
    return {"schema": CACHE_SCHEMA, "explicit": [], "implicit": []}


def _valid_cache_entry(value: Any, *, lane: str) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == _CACHE_ENTRY_KEYS
        and isinstance(value.get("fingerprint"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["fingerprint"])
        and value.get("status") in _CACHE_STATUSES[lane]
        and isinstance(value.get("checked_at"), (int, float))
        and not isinstance(value.get("checked_at"), bool)
        and math.isfinite(float(value["checked_at"]))
        and isinstance(value.get("canary_version"), int)
        and not isinstance(value.get("canary_version"), bool)
    )


def _read_cache(path: Path) -> dict[str, Any]:
    """Read and privacy-sanitize schema 2; schema 1 is always a cache miss."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return _empty_cache()
    if not isinstance(raw, dict) or raw.get("schema") != CACHE_SCHEMA:
        return _empty_cache()
    data = _empty_cache()
    for lane in _CACHE_LANES:
        entries = raw.get(lane)
        if isinstance(entries, list):
            data[lane] = [
                dict(entry)
                for entry in entries
                if _valid_cache_entry(entry, lane=lane)
            ][:MAX_CACHE_ENTRIES]
    return data


def _cache_entry(
    path: Path,
    lane: str,
    fingerprint: str,
) -> dict[str, Any] | None:
    for entry in _read_cache(path)[lane]:
        if entry["fingerprint"] == fingerprint:
            return entry
    return None


def cached_pass_age(
    path: Path,
    fingerprint: str,
    *,
    now: float,
    ttl_seconds: int,
) -> int | None:
    if ttl_seconds <= 0:
        return None
    entry = _cache_entry(path, "explicit", fingerprint)
    if entry is None or entry.get("status") != "pass":
        return None
    checked_at = entry.get("checked_at")
    if not isinstance(checked_at, (int, float)):
        return None
    age = now - float(checked_at)
    if age < -300 or age > ttl_seconds:
        return None
    return max(0, int(age))


def cached_implicit_status(
    path: Path,
    fingerprint: str,
    *,
    now: float,
    ttl_seconds: int,
) -> ImplicitStatus | None:
    """Return only the exact current-fingerprint diagnostic while fresh."""
    if ttl_seconds <= 0:
        return None
    record = implicit_cache_record(path, fingerprint)
    if record is None:
        return None
    status_value, checked_at = record
    age = now - checked_at
    if age < -300 or age > ttl_seconds:
        return None
    return status_value


def implicit_cache_record(
    path: Path,
    fingerprint: str,
) -> tuple[ImplicitStatus, float] | None:
    """Return an exact lane record, including stale data for doctor display."""
    entry = _cache_entry(path, "implicit", fingerprint)
    if entry is None:
        return None
    try:
        return ImplicitStatus(entry["status"]), float(entry["checked_at"])
    except (KeyError, TypeError, ValueError):  # pragma: no cover - sanitized above
        return None


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total >= 3600:
        return f"{total // 3600} 小時"
    if total >= 60:
        return f"{total // 60} 分鐘"
    return f"{total} 秒"


def _live_canary_reason(
    path: Path,
    fingerprint: str,
    *,
    now: float,
    ttl_seconds: int,
) -> str:
    """Explain why a live model canary is about to run (new combo vs expiry)."""
    if ttl_seconds <= 0:
        return "config.TOOL_CANARY_TTL_SECONDS=0，快取已停用"
    entry = _cache_entry(path, "explicit", fingerprint)
    if entry is not None and entry.get("status") == "pass":
        checked_at = entry.get("checked_at")
        if isinstance(checked_at, (int, float)):
            age = now - float(checked_at)
            if age > ttl_seconds:
                return (
                    f"上次通過已是約 {_format_duration(age)}前，"
                    f"超過快取期 {_format_duration(ttl_seconds)}"
                )
    return "這個專案＋模型＋設定組合尚無通過紀錄（新專案或設定變動）"


def _save_cache_entry(
    path: Path,
    fingerprint: str,
    *,
    lane: str,
    status_value: str,
    now: float,
) -> None:
    if lane not in _CACHE_LANES or status_value not in _CACHE_STATUSES.get(
        lane, frozenset()
    ):
        raise ValueError("invalid canary cache lane/status")
    if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise ValueError("canary fingerprint must be a lowercase SHA-256 hex digest")
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("canary checked_at must be a finite number")
    data = _read_cache(path)
    entries = [
        entry for entry in data[lane] if entry.get("fingerprint") != fingerprint
    ]
    entries.append({
        "fingerprint": fingerprint,
        "status": status_value,
        "checked_at": now,
        "canary_version": CANARY_VERSION,
    })
    data[lane] = sorted(
        entries,
        key=lambda entry: entry.get("checked_at", 0),
        reverse=True,
    )[:MAX_CACHE_ENTRIES]

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(stat.S_IRWXU)
    except OSError:
        pass
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            json.dump(data, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink()
            except OSError:
                pass


def save_cached_pass(path: Path, fingerprint: str, *, now: float) -> None:
    """Cache a successful explicit transport/obedience probe."""
    _save_cache_entry(
        path,
        fingerprint,
        lane="explicit",
        status_value="pass",
        now=now,
    )


def save_cached_implicit(
    path: Path,
    fingerprint: str,
    status_value: ImplicitStatus,
    *,
    now: float,
) -> None:
    """Cache one non-blocking autonomous-routing diagnostic."""
    _save_cache_entry(
        path,
        fingerprint,
        lane="implicit",
        status_value=status_value.value,
        now=now,
    )


# 事件流的解析只有一份(client_events),canary / routing eval / session_eval
# replay 共用。各自寫一份的話,同一次 run 在兩邊會得到不同判定,而兩邊都不會
# 報錯。
_event_session_ids = client_events.event_session_ids


def inspect_model_events(output: str) -> ModelEvidence:
    session_ids: list[str] = []
    saw_target = False
    saw_completed_target = False
    saw_tool_calls_finish = False

    for event in client_events.iter_events(output):
        for session_id in _event_session_ids(event):
            if session_id not in session_ids:
                session_ids.append(session_id)

        part = event.get("part")
        part = part if isinstance(part, dict) else {}
        if event.get("type") == "step_finish" or part.get("type") == "step-finish":
            reason = part.get("reason", event.get("reason"))
            if reason == "tool-calls":
                saw_tool_calls_finish = True

        if event.get("type") != "tool_use":
            continue
        tool = part.get("tool", event.get("tool"))
        if tool != TARGET_FRONTEND_TOOL:
            continue
        saw_target = True
        state = part.get("state", event.get("state"))
        state = state if isinstance(state, dict) else {}
        tool_input = state.get("input", part.get("input", event.get("input")))
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        if (
            state.get("status") == "completed"
            and tool_input.get("path") == "."
            and tool_input.get("depth") == 1
        ):
            saw_completed_target = True

    if saw_completed_target:
        return ModelEvidence(
            True,
            "收到 completed 的結構化 list_dir(path='.', depth=1)",
            tuple(session_ids),
            saw_tool_calls_finish,
        )
    if saw_target:
        reason = "收到 list_dir event，但不是 completed 或參數不符"
    else:
        reason = "沒有收到結構化 list_dir tool_use（純文字/XML 不算）"
    return ModelEvidence(False, reason, tuple(session_ids), saw_tool_calls_finish)


def inspect_implicit_events(output: str) -> ImplicitEvidence:
    """Classify only completed structured calls from the unnamed-intent probe."""
    session_ids: list[str] = []
    completed_read_only: list[tuple[str, dict[str, Any]]] = []
    for event in client_events.iter_events(output):
        for session_id in _event_session_ids(event):
            if session_id not in session_ids:
                session_ids.append(session_id)
        if event.get("type") != "tool_use":
            continue
        part = event.get("part")
        part = part if isinstance(part, dict) else {}
        state = part.get("state", event.get("state"))
        state = state if isinstance(state, dict) else {}
        if state.get("status") != "completed":
            continue
        tool = part.get("tool", event.get("tool"))
        if tool not in IMPLICIT_READ_ONLY_FRONTEND_TOOLS:
            continue
        tool_input = state.get("input", part.get("input", event.get("input")))
        completed_read_only.append(
            (tool, tool_input if isinstance(tool_input, dict) else {})
        )

    for tool, tool_input in completed_read_only:
        if tool == TARGET_FRONTEND_TOOL and tool_input.get("path") in (".", "", "./"):
            return ImplicitEvidence(ImplicitStatus.OPTIMAL, tuple(session_ids))
    if completed_read_only:
        return ImplicitEvidence(ImplicitStatus.SUBOPTIMAL, tuple(session_ids))
    return ImplicitEvidence(ImplicitStatus.FAIL, tuple(session_ids))


def _coerce_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_model_attempt(
    *,
    root: Path,
    env: Mapping[str, str],
    model_override: str,
    timeout: int,
) -> ModelEvidence:
    command = _model_canary_command(
        root=root,
        model_override=model_override,
        title="CodeTrail explicit tool-call canary",
        prompt=CANARY_PROMPT,
        env=env,
    )
    try:
        result = _run_process_with_heartbeat(command, root=root, timeout=timeout)
    except FileNotFoundError:
        return ModelEvidence(False, "找不到 python 直譯器或客戶端進入點")
    except OSError as exc:
        return ModelEvidence(False, f"headless run 無法啟動（{type(exc).__name__}）")
    except process_env.TimeoutExpired as exc:
        evidence = inspect_model_events(_coerce_text(exc.stdout))
        return replace(evidence, success=False, reason=f"headless run 超過 {timeout} 秒",
                       failure_kind="timeout")

    evidence = inspect_model_events(result.stdout)
    if result.returncode != 0:
        return replace(
            evidence,
            success=False,
            reason=f"headless run exit {result.returncode}（輸出已避免顯示）",
        )
    return evidence


def _model_canary_command(
    *,
    root: Path,
    model_override: str,
    title: str,
    prompt: str,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """真的跑一次 headless 客戶端。

    ``--policy readonly``:canary 絕不能寫到使用者的專案。
    **不帶** ``--persist``:headless 預設 ephemeral,所以抽查不會在 session
    清單裡留下對話。``title`` 保留在簽名裡是為了呼叫端可讀,ephemeral 的
    session 沒有標題可存。
    """
    del title  # ephemeral session 沒有要存的標題
    command = [
        sys.executable,
        str(client_entry()),
        "run",
        "--root",
        str(root),
        "--policy",
        "readonly",
    ]
    if model_override:
        # 不帶的話 canary --model 指定的模型根本沒送出去,抽查的是別顆模型。
        command.extend(["--model", model_override])
    command.extend(["--format", "json", prompt])
    return command


def run_implicit_model_attempt(
    *,
    root: Path,
    env: Mapping[str, str],
    model_override: str,
    timeout: int,
) -> ImplicitEvidence:
    command = _model_canary_command(
        root=root,
        model_override=model_override,
        title="CodeTrail implicit routing diagnostic",
        prompt=IMPLICIT_CANARY_PROMPT,
        env=env,
    )
    try:
        result = _run_process_with_heartbeat(command, root=root, timeout=timeout)
    except FileNotFoundError:
        return ImplicitEvidence(ImplicitStatus.FAIL)
    except OSError:
        return ImplicitEvidence(ImplicitStatus.FAIL)
    except process_env.TimeoutExpired as exc:
        partial = inspect_implicit_events(_coerce_text(exc.stdout))
        return ImplicitEvidence(ImplicitStatus.TIMEOUT, partial.session_ids)
    if result.returncode != 0:
        failed = inspect_implicit_events(result.stdout)
        return ImplicitEvidence(ImplicitStatus.FAIL, failed.session_ids)
    return inspect_implicit_events(result.stdout)


def _model_selection(env: Mapping[str, str], explicit: str) -> tuple[str, str]:
    """Return (selected model for the fingerprint, model to run the probe with).

    `explicit` 是呼叫端(preflight)已經解析好的主模型 —— canary 驗的必須就是
    等一下真的要跑的那一顆。沒給就自己從 deployment profile / models.json 解析
    一次;`env` 只用來定位那些檔(HOME),不是設定來源。
    """
    if explicit:
        return explicit, explicit
    resolved = resolve_main_model(dict(env))
    if not resolved.ok or not resolved.model:
        raise CanaryError(
            "找不到主模型(deployment profile 沒有 main.model),且呼叫端未指定模型"
        )
    return resolved.model, resolved.model


def _handle_failure(message: str, *, timed_out: bool = False) -> int:
    _print(f"FAIL — {message}", error=True)
    _print(
        ("已拒絕啟動：explicit 模型在時限內未完成驗證，不能據此判定工具契約損壞。"
         "請檢查模型是否忙碌、單 slot 排程等待或處理緩慢，待服務可用後重試。"
         if timed_out else "已拒絕啟動；請修正 direct-tool / MCP / explicit tool-call 契約後重試"),
        error=True,
    )
    return 2


def _report_implicit(status_value: ImplicitStatus, *, cached: bool) -> None:
    source = "cached" if cached else "live"
    if status_value is ImplicitStatus.OPTIMAL:
        _print(f"IMPLICIT {source} — status=optimal（自主選到根目錄列舉）")
        return
    if status_value is ImplicitStatus.TIMEOUT:
        _print(
            f"IMPLICIT WARN — status=timeout（{source}；時限內未完成診斷，不等於 routing 失敗；不擋啟動）",
            error=True,
        )
        return
    _print(
        f"IMPLICIT WARN — status={status_value.value}（{source}；不擋啟動）",
        error=True,
    )


def run_all(
    *,
    root: Path,
    env: Mapping[str, str],
    explicit_model: str,
    base_url: str,
    force: bool,
) -> int:
    """`env` 只是**檔案位置**(HOME / XDG_CACHE_HOME)與子行程要繼承的使用者環境;
    每一個設定值(模型、endpoint、逾時、TTL)都由參數或 `config.py` 常數決定。"""
    mcp_timeout = TOOL_CANARY_MCP_TIMEOUT_SECONDS
    model_timeout = TOOL_CANARY_MODEL_TIMEOUT_SECONDS
    implicit_timeout = TOOL_CANARY_IMPLICIT_TIMEOUT_SECONDS
    ttl_seconds = TOOL_CANARY_TTL_SECONDS
    try:
        protocol_evidence = run_protocol_check(root=root, timeout=mcp_timeout)
    except CanaryError as exc:
        return _handle_failure(str(exc))

    if not isinstance(protocol_evidence, ProtocolEvidence):
        return _handle_failure(
            "MCP protocol check 沒有回傳 live tools/instructions fingerprint evidence"
        )
    _print(f"MCP PASS — {len(EXPECTED_MCP_TOOLS)} tools + list_dir round-trip")

    try:
        selected_model, model_override = _model_selection(env, explicit_model)
    except CanaryError as exc:
        return _handle_failure(str(exc))

    try:
        props = fetch_main_server_props(base_url)
        import config
        if config.DEPLOYMENT_MODE == "client":
            from model_identity import capture_model_identity
            if not isinstance(props, dict):
                return _handle_failure("client topology requires live main identity before model canary")
            capture_model_identity("main", props=props)
    except RuntimeError as exc:
        return _handle_failure(str(exc))
    caps = props.get("chat_template_caps") if isinstance(props, Mapping) else None
    if isinstance(caps, Mapping) and caps.get("supports_tools") is False:
        return _handle_failure(
            "llama-server /props 明確回報 chat_template_caps.supports_tools=false；"
            "未執行任何 model canary",
        )
    cache_path = resolve_cache_path(env)
    cache_ready = props is not None and cache_path is not None
    fingerprint = ""
    bypass_cache = force
    explicit_cached = False
    if cache_ready:
        assert props is not None
        fingerprint = build_fingerprint(
            root=root,
            selected_model=selected_model,
            props=props,
            env=env,
            protocol_evidence=protocol_evidence,
        )
        if bypass_cache:
            live_reason = "--force 略過快取"
        else:
            age = cached_pass_age(
                cache_path,
                fingerprint,
                now=time.time(),
                ttl_seconds=ttl_seconds,
            )
            if age is not None:
                _print(f"MODEL PASS — cached structured tool_use（{age // 60} 分鐘前）")
                explicit_cached = True
                live_reason = ""
            else:
                live_reason = _live_canary_reason(
                    cache_path,
                    fingerprint,
                    now=time.time(),
                    ttl_seconds=ttl_seconds,
                )
    else:
        live_reason = "server /props 或快取路徑不可用；本次結果不會快取"

    if not explicit_cached:
        # 這一步是整個 aicode 啟動流程唯一會安靜跑數十秒以上的地方；先講清楚
        # 原因與預期時長，執行中再配合 heartbeat，避免被誤判成當機。
        _print(f"MODEL live canary — {live_reason}")
        _print(
            "現在實跑 explicit headless run（唯讀權限、不落 session），"
            "驗證模型會真的呼叫 list_dir；"
            f"單次上限 {model_timeout} 秒，執行中每 "
            f"{MODEL_CANARY_HEARTBEAT_SECONDS} 秒回報進度，不是當機。"
        )
        last_reason = "未知錯誤"
        timed_out = False
        explicit_passed = False
        for attempt in (1, 2):
            evidence = run_model_attempt(
                root=root,
                env=env,
                model_override=model_override,
                timeout=model_timeout,
            )
            # headless 預設 ephemeral:canary 的對話從來沒有落檔,所以沒有
            # 「刪掉暫存 session」這一步。事件流仍帶精確 session id,只是那個
            # id 不對應任何檔案。

            if evidence.success:
                suffix = " + tool-calls finish" if evidence.saw_tool_calls_finish else ""
                if attempt == 1:
                    if cache_ready and fingerprint:
                        try:
                            assert cache_path is not None
                            save_cached_pass(cache_path, fingerprint, now=time.time())
                        except OSError:
                            _print(
                                "WARNING — explicit PASS，但快取寫入失敗；下次會重測",
                                error=True,
                            )
                    _print(
                        f"MODEL PASS — structured {TARGET_FRONTEND_TOOL} completed{suffix}"
                    )
                else:
                    _print(
                        "MODEL FLAKY — explicit 第二次才成功；本次允許啟動但不快取，"
                        "下次 aicode 會再測",
                        error=True,
                    )
                explicit_passed = True
                break

            last_reason = evidence.reason
            timed_out = timed_out or evidence.failure_kind == "timeout"
            if attempt == 1:
                _print(f"MODEL RETRY — 第一次失敗：{last_reason}", error=True)

        if not explicit_passed:
            return _handle_failure(
                "MCP protocol 已通過，但 explicit 模型連續兩次未完成真實 tool call："
                + last_reason,
                timed_out=timed_out,
            )

    if cache_ready and fingerprint and not bypass_cache:
        assert cache_path is not None
        implicit_cached = cached_implicit_status(
            cache_path,
            fingerprint,
            now=time.time(),
            ttl_seconds=ttl_seconds,
        )
        if implicit_cached is not None:
            _report_implicit(implicit_cached, cached=True)
            return 0

    _print(
        "IMPLICIT live diagnostic — 以未點名工具的目錄意圖檢查自主 routing；"
        f"只跑一次、上限 {implicit_timeout} 秒，任何結果都不擋啟動"
    )
    implicit = run_implicit_model_attempt(
        root=root,
        env=env,
        model_override=model_override,
        timeout=implicit_timeout,
    )
    if cache_ready and fingerprint:
        try:
            assert cache_path is not None
            save_cached_implicit(
                cache_path,
                fingerprint,
                implicit.status,
                now=time.time(),
            )
        except OSError:
            _print("WARNING — implicit 診斷快取寫入失敗；下次會重測", error=True)
    _report_implicit(implicit.status, cached=False)
    return 0


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="sandbox/project root；預設 cwd")
    parser.add_argument(
        "--model",
        default="",
        help="要驗的主模型(registry 名或 GGUF 路徑);省略就從 deployment profile 解析",
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="主 llama-server URL;省略就從 deployment profile 取",
    )
    parser.add_argument("--force", action="store_true", help="ignore a valid model-canary cache")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    import client_config
    client_config.apply_to_config(client_config.load_client_settings(), readonly=True)
    # 子行程的環境:剝掉全部 CodeTrail 設定變數。standalone 執行時這一步特別
    # 重要 —— preflight 那條路的呼叫端是客戶端(它自己已經不看那些變數),
    # 但直接跑這支的人可能就站在一個污染的殼層裡。
    env = client_mcp.child_env()

    raw_root = args.root or os.getcwd()
    try:
        root = Path(raw_root).expanduser().resolve(strict=True)
    except (OSError, ValueError) as exc:
        return _handle_failure(f"canary root 無法解析：{type(exc).__name__}")
    if not root.is_dir():
        return _handle_failure("canary root 不是目錄")

    base_url = args.base_url.strip()
    if not base_url:
        try:
            base_url = default_base_url()
        except CanaryError as exc:
            return _handle_failure(str(exc))
    return run_all(
        root=root,
        env=env,
        explicit_model=args.model.strip(),
        base_url=base_url,
        force=args.force,
    )


if __name__ == "__main__":
    raise SystemExit(main())
