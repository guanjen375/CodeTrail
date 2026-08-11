#!/usr/bin/env python3
"""Verify CodeTrail MCP wiring and the active model's real tool-call path.

``aicode`` runs this before starting OpenCode.  The protocol check is always
live: it starts the effective ``mcp.codetrail.command``, performs
initialize/tools/list, verifies the exact public tool contract, and calls the
read-only ``list_dir`` tool.  The model check starts a fresh ``opencode run``
session and accepts only a structured, completed ``codetrail_list_dir`` event;
assistant prose or XML that merely looks like a tool call never counts.

The model check is comparatively slow, so successful results are cached by a
configuration/runtime fingerprint.  Only the fingerprint and timestamp are
stored -- never prompts, model output, tool output, project paths, or config
contents.  Transient OpenCode canary sessions are deleted after inspection.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from model_resolution import parse_cli_model_arg_detail  # noqa: E402

CANARY_VERSION = 1
CACHE_SCHEMA = 1
DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60
DEFAULT_MCP_TIMEOUT_SECONDS = 90
DEFAULT_MODEL_TIMEOUT_SECONDS = 240
DEFAULT_CONFIG_TIMEOUT_SECONDS = 30
MODEL_CANARY_HEARTBEAT_SECONDS = 15
MAX_CACHE_ENTRIES = 32
MAX_PROPS_BYTES = 4 * 1024 * 1024

EXPECTED_MCP_TOOLS = frozenset(
    {
        "analyze_file",
        "apply_patch",
        "code_rag_search",
        "file_info",
        "git_diff",
        "git_status",
        "grep_code",
        "import_external_file",
        "ingest_document",
        "list_dir",
        "query_knowledge",
        "query_knowledge_strict",
        "read_file",
        "reload_knowledge_base",
        "remove_document",
        "run_command",
        "run_lint",
    }
)
TARGET_FRONTEND_TOOL = "codetrail_list_dir"
CANARY_PROMPT = (
    "請立即呼叫 codetrail_list_dir，path=\".\"、depth=1。"
    "必須實際呼叫工具，不准用文字回答工具是否存在。"
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


class CanaryError(RuntimeError):
    """An actionable health-check failure whose message is safe to print."""


@dataclass(frozen=True)
class McpCommand:
    argv: tuple[str, ...]
    environment: dict[str, str]


@dataclass(frozen=True)
class ModelEvidence:
    success: bool
    reason: str
    session_ids: tuple[str, ...] = ()
    saw_tool_calls_finish: bool = False


def _print(message: str, *, error: bool = False) -> None:
    print(f"[tool-health] {message}", file=sys.stderr if error else sys.stdout, flush=True)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def _env_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise CanaryError(f"{name} 必須是整數，實得 {raw!r}") from exc
    if value < minimum or value > maximum:
        raise CanaryError(f"{name} 必須介於 {minimum}..{maximum}，實得 {value}")
    return value


def _run_process(
    argv: Sequence[str],
    *,
    root: Path,
    env: Mapping[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=str(root),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _run_process_with_heartbeat(
    argv: Sequence[str],
    *,
    root: Path,
    env: Mapping[str, str],
    timeout: int,
    heartbeat: float = MODEL_CANARY_HEARTBEAT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """``_run_process`` with periodic progress lines while the child runs.

    The live model canary regularly takes tens of seconds on local hardware;
    with zero output users assume ``aicode`` is hung.  Timeout behaviour
    matches ``_run_process``: raise ``subprocess.TimeoutExpired`` carrying any
    partial output collected so far.
    """
    with subprocess.Popen(
        list(argv),
        cwd=str(root),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        started = time.monotonic()
        try:
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    process.kill()
                    stdout, stderr = process.communicate()
                    raise subprocess.TimeoutExpired(
                        list(argv), timeout, output=stdout, stderr=stderr
                    )
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(heartbeat, remaining)
                    )
                except subprocess.TimeoutExpired:
                    elapsed = int(time.monotonic() - started)
                    _print(
                        f"opencode run 仍在執行… 已 {elapsed} 秒"
                        f"（單次上限 {timeout} 秒）"
                    )
                    continue
                return subprocess.CompletedProcess(
                    list(argv), process.returncode, stdout, stderr
                )
        except BaseException:
            process.kill()
            raise


def load_effective_opencode_config(
    root: Path,
    env: Mapping[str, str],
    *,
    timeout: int = DEFAULT_CONFIG_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Ask OpenCode for the merged config rather than guessing merge order."""
    try:
        result = _run_process(
            ["opencode", "debug", "config"],
            root=root,
            env=env,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CanaryError("找不到 opencode，無法解析實際生效設定") from exc
    except subprocess.TimeoutExpired as exc:
        raise CanaryError(f"opencode debug config 超過 {timeout} 秒") from exc

    if result.returncode != 0:
        raise CanaryError(
            f"opencode debug config 失敗（exit {result.returncode}）；"
            "請先直接執行該命令檢查設定"
        )
    try:
        config = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CanaryError("opencode debug config 沒有回傳合法 JSON") from exc
    if not isinstance(config, dict):
        raise CanaryError("opencode debug config 的根節點不是 JSON object")
    return config


def extract_codetrail_command(
    config: Mapping[str, Any],
    *,
    root: Path,
) -> McpCommand:
    mcp = config.get("mcp")
    entry = mcp.get("codetrail") if isinstance(mcp, dict) else None
    if not isinstance(entry, dict):
        raise CanaryError("實際 OpenCode 設定沒有 mcp.codetrail entry")
    if entry.get("enabled") is False:
        raise CanaryError("實際 OpenCode 設定把 mcp.codetrail.enabled 關閉了")
    if entry.get("type") not in (None, "local"):
        raise CanaryError("mcp.codetrail 不是 local stdio MCP，無法執行本機 protocol canary")

    command = entry.get("command")
    if not (
        isinstance(command, list)
        and command
        and all(isinstance(part, str) and part for part in command)
    ):
        raise CanaryError("mcp.codetrail.command 必須是非空字串陣列")

    raw_environment = entry.get("environment", {})
    if not isinstance(raw_environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw_environment.items()
    ):
        raise CanaryError("mcp.codetrail.environment 必須是字串對字串的 object")
    configured_root = raw_environment.get("AICODE_ROOT")
    if configured_root:
        try:
            configured_path = Path(configured_root).expanduser().resolve()
        except (OSError, ValueError) as exc:
            raise CanaryError("mcp.codetrail.environment.AICODE_ROOT 無法解析") from exc
        if configured_path != root:
            raise CanaryError(
                "mcp.codetrail.environment.AICODE_ROOT 與本次 aicode sandbox root 不同"
            )

    return McpCommand(tuple(command), dict(raw_environment))


async def _mcp_roundtrip(
    command: McpCommand,
    *,
    root: Path,
    env: Mapping[str, str],
) -> tuple[set[str], bool]:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise CanaryError("目前 Python 找不到 mcp 套件，無法做 protocol canary") from exc

    server_env = dict(env)
    server_env.update(command.environment)
    server_env["AICODE_ROOT"] = str(root)
    params = StdioServerParameters(
        command=command.argv[0],
        args=list(command.argv[1:]),
        env=server_env,
        cwd=root,
    )

    # MCP server startup logs may include project-local filenames.  Keep them
    # in an anonymous temporary file and never echo/store them on success.
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed_tools = await session.list_tools()
                names = {tool.name for tool in listed_tools.tools}
                result = await session.call_tool("list_dir", {"path": ".", "depth": 1})
                return names, bool(getattr(result, "isError", False))


def run_protocol_check(
    config: Mapping[str, Any],
    *,
    root: Path,
    env: Mapping[str, str],
    timeout: int,
) -> None:
    command = extract_codetrail_command(config, root=root)
    try:
        names, list_dir_error = asyncio.run(
            asyncio.wait_for(
                _mcp_roundtrip(command, root=root, env=env),
                timeout=timeout,
            )
        )
    except TimeoutError as exc:
        raise CanaryError(f"MCP initialize/tools/list/list_dir 超過 {timeout} 秒") from exc
    except CanaryError:
        raise
    except Exception as exc:
        raise CanaryError(
            "MCP initialize/tools/list/list_dir 失敗"
            f"（{type(exc).__name__}；server 詳細 log 已避免輸出）"
        ) from exc

    missing = sorted(EXPECTED_MCP_TOOLS - names)
    unexpected = sorted(names - EXPECTED_MCP_TOOLS)
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"缺少={missing}")
        if unexpected:
            details.append(f"未同步新增={unexpected}")
        raise CanaryError(
            f"MCP 工具 contract 不符：預期 {len(EXPECTED_MCP_TOOLS)}、實得 {len(names)}；"
            + "；".join(details)
        )
    if list_dir_error:
        raise CanaryError("MCP list_dir round-trip 回傳 isError=true")


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
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"


def _server_root_url(base_url: str) -> str:
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return root


def fetch_main_server_props(
    env: Mapping[str, str],
    *,
    timeout: int = 5,
) -> dict[str, Any] | None:
    base_url = (env.get("AICODE_LLAMA_BASE_URL") or "http://localhost:8080").strip()
    url = _server_root_url(base_url) + "/props"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
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


def read_opencode_version(root: Path, env: Mapping[str, str]) -> str | None:
    try:
        result = _run_process(
            ["opencode", "--version"],
            root=root,
            env=env,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _model_file_signature(props: Mapping[str, Any]) -> dict[str, Any]:
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


def build_fingerprint(
    *,
    root: Path,
    config: Mapping[str, Any],
    selected_model: str,
    props: Mapping[str, Any],
    opencode_version: str,
    env: Mapping[str, str],
) -> str:
    home_raw = (env.get("HOME") or env.get("USERPROFILE") or "").strip()
    home = Path(home_raw).expanduser() if home_raw else None
    tracked_files = {
        "canary": _file_digest(Path(__file__)),
        "mcp_server": _file_digest(REPO_ROOT / "mcp_server.py"),
        "project_agents": _file_digest(root / "AGENTS.md"),
        "project_opencode_json": _file_digest(root / ".opencode" / "opencode.json"),
        "project_opencode_jsonc": _file_digest(root / ".opencode" / "opencode.jsonc"),
    }
    if home is not None:
        tracked_files["global_agents"] = _file_digest(
            home / ".config" / "opencode" / "AGENTS.md"
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
    payload = {
        "canary_version": CANARY_VERSION,
        "root": str(root),
        "selected_model": selected_model,
        "opencode_version": opencode_version,
        # Hash the resolved config inside the final digest.  This includes all
        # effective provider/MCP/agent settings without persisting credentials.
        "effective_config_hash": _json_digest(config),
        "props": props_subset,
        "model_file": _model_file_signature(props),
        "files": tracked_files,
    }
    return _json_digest(payload)


def resolve_cache_path(env: Mapping[str, str]) -> Path | None:
    explicit = (env.get("AICODE_TOOL_CANARY_CACHE") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = (env.get("XDG_CACHE_HOME") or "").strip()
    if xdg:
        return Path(xdg).expanduser() / "codetrail" / "tool-call-canary.json"
    home = (env.get("HOME") or env.get("USERPROFILE") or "").strip()
    if not home:
        return None
    return Path(home).expanduser() / ".cache" / "codetrail" / "tool-call-canary.json"


def _read_cache(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"schema": CACHE_SCHEMA, "passes": {}}
    if not isinstance(data, dict) or data.get("schema") != CACHE_SCHEMA:
        return {"schema": CACHE_SCHEMA, "passes": {}}
    if not isinstance(data.get("passes"), dict):
        data["passes"] = {}
    return data


def cached_pass_age(
    path: Path,
    fingerprint: str,
    *,
    now: float,
    ttl_seconds: int,
) -> int | None:
    if ttl_seconds <= 0:
        return None
    entry = _read_cache(path).get("passes", {}).get(fingerprint)
    if not isinstance(entry, dict) or entry.get("status") != "pass":
        return None
    checked_at = entry.get("checked_at")
    if not isinstance(checked_at, (int, float)):
        return None
    age = now - float(checked_at)
    if age < -300 or age > ttl_seconds:
        return None
    return max(0, int(age))


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
        return "AICODE_TOOL_CANARY_TTL_SECONDS=0，快取已停用"
    entry = _read_cache(path).get("passes", {}).get(fingerprint)
    if isinstance(entry, dict) and entry.get("status") == "pass":
        checked_at = entry.get("checked_at")
        if isinstance(checked_at, (int, float)):
            age = now - float(checked_at)
            if age > ttl_seconds:
                return (
                    f"上次通過已是約 {_format_duration(age)}前，"
                    f"超過快取期 {_format_duration(ttl_seconds)}"
                )
    return "這個專案＋模型＋設定組合尚無通過紀錄（新專案或設定變動）"


def save_cached_pass(path: Path, fingerprint: str, *, now: float) -> None:
    data = _read_cache(path)
    passes = data.setdefault("passes", {})
    assert isinstance(passes, dict)
    passes[fingerprint] = {
        "status": "pass",
        "checked_at": now,
        "canary_version": CANARY_VERSION,
    }
    if len(passes) > MAX_CACHE_ENTRIES:
        ordered = sorted(
            passes.items(),
            key=lambda item: (
                item[1].get("checked_at", 0) if isinstance(item[1], dict) else 0
            ),
            reverse=True,
        )
        data["passes"] = dict(ordered[:MAX_CACHE_ENTRIES])

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


def _event_session_ids(event: Mapping[str, Any]) -> list[str]:
    candidates = [event.get("sessionID"), event.get("session_id")]
    part = event.get("part")
    if isinstance(part, dict):
        candidates.extend((part.get("sessionID"), part.get("session_id")))
    return [
        value
        for value in candidates
        if isinstance(value, str) and _SAFE_SESSION_ID.fullmatch(value)
    ]


def inspect_model_events(output: str) -> ModelEvidence:
    session_ids: list[str] = []
    saw_target = False
    saw_completed_target = False
    saw_tool_calls_finish = False

    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
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
            "收到 completed 的結構化 codetrail_list_dir(path='.', depth=1)",
            tuple(session_ids),
            saw_tool_calls_finish,
        )
    if saw_target:
        reason = "收到 codetrail_list_dir event，但不是 completed 或參數不符"
    else:
        reason = "沒有收到結構化 codetrail_list_dir tool_use（純文字/XML 不算）"
    return ModelEvidence(False, reason, tuple(session_ids), saw_tool_calls_finish)


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
    command = [
        "opencode",
        "run",
        "--dir",
        str(root),
        "--agent",
        "build",
        "--format",
        "json",
        "--title",
        "CodeTrail tool-call canary",
    ]
    if model_override:
        command.extend(("--model", model_override))
    command.append(CANARY_PROMPT)
    try:
        result = _run_process_with_heartbeat(command, root=root, env=env, timeout=timeout)
    except FileNotFoundError:
        return ModelEvidence(False, "找不到 opencode")
    except subprocess.TimeoutExpired as exc:
        evidence = inspect_model_events(_coerce_text(exc.stdout))
        return replace(evidence, success=False, reason=f"opencode run 超過 {timeout} 秒")

    evidence = inspect_model_events(result.stdout)
    if result.returncode != 0:
        return replace(
            evidence,
            success=False,
            reason=f"opencode run exit {result.returncode}（輸出已避免顯示）",
        )
    return evidence


def delete_canary_sessions(
    session_ids: Sequence[str],
    *,
    root: Path,
    env: Mapping[str, str],
) -> bool:
    ok = True
    for session_id in dict.fromkeys(session_ids):
        if not _SAFE_SESSION_ID.fullmatch(session_id):
            continue
        try:
            result = _run_process(
                ["opencode", "session", "delete", session_id],
                root=root,
                env=env,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            ok = False
            continue
        ok = ok and result.returncode == 0
    return ok


def _model_selection(
    config: Mapping[str, Any], explicit: str, frontend_args: Sequence[str]
) -> tuple[str, str]:
    """Return (selected model for fingerprint, CLI override for opencode run)."""
    if explicit:
        return explicit, explicit
    parsed = parse_cli_model_arg_detail(frontend_args)
    if parsed.error:
        raise CanaryError(parsed.error)
    if parsed.values:
        selected = parsed.values[-1]
        return selected, selected
    configured = config.get("model")
    if not isinstance(configured, str) or not configured.strip():
        raise CanaryError("OpenCode 實際設定沒有 active model，且本次未傳 -m/--model")
    return configured.strip(), ""


def _handle_failure(message: str, *, warn_only: bool) -> int:
    _print(f"FAIL — {message}", error=True)
    if warn_only:
        _print("WARN-ONLY — 依 AICODE_TOOL_CANARY_WARN_ONLY=1 繼續啟動", error=True)
        return 0
    _print(
        "已拒絕啟動；若只是要進 TUI 排查，可暫用 "
        "AICODE_TOOL_CANARY_WARN_ONLY=1 aicode（不要把它當永久修法）",
        error=True,
    )
    return 2


def run_all(
    *,
    root: Path,
    env: Mapping[str, str],
    explicit_model: str,
    frontend_args: Sequence[str],
    force: bool,
) -> int:
    warn_only = _truthy(env.get("AICODE_TOOL_CANARY_WARN_ONLY"))
    try:
        config_timeout = _env_int(
            env,
            "AICODE_TOOL_CANARY_CONFIG_TIMEOUT_SECONDS",
            DEFAULT_CONFIG_TIMEOUT_SECONDS,
            minimum=5,
            maximum=300,
        )
        mcp_timeout = _env_int(
            env,
            "AICODE_TOOL_CANARY_MCP_TIMEOUT_SECONDS",
            DEFAULT_MCP_TIMEOUT_SECONDS,
            minimum=10,
            maximum=900,
        )
        model_timeout = _env_int(
            env,
            "AICODE_TOOL_CANARY_MODEL_TIMEOUT_SECONDS",
            DEFAULT_MODEL_TIMEOUT_SECONDS,
            minimum=30,
            maximum=1800,
        )
        ttl_seconds = _env_int(
            env,
            "AICODE_TOOL_CANARY_TTL_SECONDS",
            DEFAULT_CACHE_TTL_SECONDS,
            minimum=0,
            maximum=30 * 24 * 60 * 60,
        )
        config = load_effective_opencode_config(root, env, timeout=config_timeout)
        run_protocol_check(config, root=root, env=env, timeout=mcp_timeout)
    except CanaryError as exc:
        return _handle_failure(str(exc), warn_only=warn_only)

    _print(f"MCP PASS — {len(EXPECTED_MCP_TOOLS)} tools + list_dir round-trip")

    try:
        selected_model, model_override = _model_selection(
            config, explicit_model, frontend_args
        )
    except CanaryError as exc:
        return _handle_failure(str(exc), warn_only=warn_only)

    props = fetch_main_server_props(env)
    opencode_version = read_opencode_version(root, env)
    cache_path = resolve_cache_path(env)
    cache_ready = props is not None and opencode_version is not None and cache_path is not None
    fingerprint = ""
    if cache_ready:
        assert props is not None and opencode_version is not None
        fingerprint = build_fingerprint(
            root=root,
            config=config,
            selected_model=selected_model,
            props=props,
            opencode_version=opencode_version,
            env=env,
        )
        if force or _truthy(env.get("AICODE_TOOL_CANARY_FORCE")):
            live_reason = "--force／AICODE_TOOL_CANARY_FORCE 略過快取"
        else:
            age = cached_pass_age(
                cache_path,
                fingerprint,
                now=time.time(),
                ttl_seconds=ttl_seconds,
            )
            if age is not None:
                _print(f"MODEL PASS — cached structured tool_use（{age // 60} 分鐘前）")
                return 0
            live_reason = _live_canary_reason(
                cache_path,
                fingerprint,
                now=time.time(),
                ttl_seconds=ttl_seconds,
            )
    else:
        live_reason = "server /props、opencode 版本或快取路徑不可用；本次結果不會快取"

    # 這一步是整個 aicode 啟動流程唯一會安靜跑數十秒以上的地方；先講清楚
    # 原因與預期時長，執行中再配合 heartbeat，避免被誤判成當機。
    _print(f"MODEL live canary — {live_reason}")
    _print(
        "現在實跑一次 opencode run，驗證模型會真的呼叫 codetrail_list_dir；"
        f"本地推理通常需要數十秒到數分鐘（單次上限 {model_timeout} 秒），"
        f"執行中每 {MODEL_CANARY_HEARTBEAT_SECONDS} 秒回報進度，不是當機。"
    )

    last_reason = "未知錯誤"
    for attempt in (1, 2):
        evidence = run_model_attempt(
            root=root,
            env=env,
            model_override=model_override,
            timeout=model_timeout,
        )
        if evidence.session_ids and not delete_canary_sessions(
            evidence.session_ids,
            root=root,
            env=env,
        ):
            _print("WARNING — 無法刪除本次暫存 OpenCode canary session", error=True)

        if evidence.success:
            suffix = " + tool-calls finish" if evidence.saw_tool_calls_finish else ""
            if attempt == 1:
                if cache_ready and fingerprint:
                    try:
                        assert cache_path is not None
                        save_cached_pass(cache_path, fingerprint, now=time.time())
                    except OSError:
                        _print("WARNING — canary PASS，但快取寫入失敗；下次會重測", error=True)
                _print(f"MODEL PASS — structured codetrail_list_dir completed{suffix}")
            else:
                _print(
                    "MODEL FLAKY — 第二次才成功；本次允許啟動但不快取，"
                    "下次 aicode 會再測",
                    error=True,
                )
            return 0

        last_reason = evidence.reason
        if attempt == 1:
            _print(f"MODEL RETRY — 第一次失敗：{last_reason}", error=True)

    return _handle_failure(
        "MCP protocol 已通過，但模型連續兩次未完成真實 tool call：" + last_reason,
        warn_only=warn_only,
    )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="sandbox/project root；預設 AICODE_ROOT 或 cwd")
    parser.add_argument("--model", default="", help="OpenCode provider/model override")
    parser.add_argument("--force", action="store_true", help="ignore a valid model-canary cache")
    parser.add_argument(
        "frontend_args",
        nargs=argparse.REMAINDER,
        help="arguments after -- are scanned only for -m/--model",
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    env = os.environ.copy()
    if _truthy(env.get("AICODE_TOOL_CANARY_SKIP")):
        _print("SKIP — AICODE_TOOL_CANARY_SKIP=1（MCP 與模型 canary 都未執行）")
        return 0

    raw_root = args.root or env.get("AICODE_ROOT") or os.getcwd()
    try:
        root = Path(raw_root).expanduser().resolve(strict=True)
    except (OSError, ValueError) as exc:
        return _handle_failure(f"canary root 無法解析：{type(exc).__name__}", warn_only=False)
    if not root.is_dir():
        return _handle_failure("canary root 不是目錄", warn_only=False)

    frontend_args = list(args.frontend_args)
    if frontend_args[:1] == ["--"]:
        frontend_args = frontend_args[1:]
    return run_all(
        root=root,
        env=env,
        explicit_model=args.model.strip(),
        frontend_args=frontend_args,
        force=args.force,
    )


if __name__ == "__main__":
    raise SystemExit(main())
