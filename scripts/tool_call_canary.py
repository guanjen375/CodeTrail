#!/usr/bin/env python3
"""Verify CodeTrail MCP wiring and the active model's real tool-call path.

``aicode`` runs this before starting OpenCode.  The protocol check is always
live: it starts the effective ``mcp.codetrail.command``, performs
initialize/tools/list, verifies the exact public tool contract, and calls the
read-only ``list_dir`` tool.  A named explicit model probe is a hard gate and
may retry once; a separate unnamed-intent probe runs once and records an
optimal/suboptimal/fail/timeout diagnostic without blocking startup.

The model checks are comparatively slow, so results are cached in separate
explicit/implicit lanes by a configuration/runtime fingerprint.  Each record
contains only fingerprint, status, time, and canary version -- never prompts,
model/tool output, project paths, session ids, or config contents.  Transient
OpenCode canary sessions are deleted after inspection.
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
import subprocess
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

import compaction_mode  # noqa: E402
from mcp_contract import PUBLIC_TOOL_NAMES, PUBLIC_TOOL_ORDER  # noqa: E402
from model_resolution import parse_cli_model_arg_detail  # noqa: E402
from scripts.opencode_direct_contract import (  # noqa: E402
    DirectToolContractError,
    require_direct_tool_contract,
)
from scripts.opencode_mcp_timeout_check import resolve_config_path  # noqa: E402

CANARY_VERSION = 2
CACHE_SCHEMA = 2
DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60
DEFAULT_MCP_TIMEOUT_SECONDS = 90
DEFAULT_EXPLICIT_TIMEOUT_SECONDS = 120
# Backward-compatible name for callers that previously had only one model lane.
DEFAULT_MODEL_TIMEOUT_SECONDS = DEFAULT_EXPLICIT_TIMEOUT_SECONDS
DEFAULT_IMPLICIT_TIMEOUT_SECONDS = 180
DEFAULT_CONFIG_TIMEOUT_SECONDS = 30
MODEL_CANARY_HEARTBEAT_SECONDS = 15
MAX_CACHE_ENTRIES = 32
MAX_PROPS_BYTES = 4 * 1024 * 1024

EXPECTED_MCP_TOOLS = PUBLIC_TOOL_NAMES
EXPECTED_MCP_TOOL_ORDER = PUBLIC_TOOL_ORDER
TARGET_FRONTEND_TOOL = "codetrail_list_dir"
CANARY_PROMPT = (
    "請立即呼叫 codetrail_list_dir，path=\".\"、depth=1。"
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
        "codetrail_list_dir",
        "codetrail_read_file",
        "codetrail_grep_code",
        "codetrail_code_rag_search",
        "codetrail_file_info",
        "codetrail_query_knowledge",
        "codetrail_query_knowledge_strict",
        "codetrail_git_status",
        "codetrail_git_diff",
        "codetrail_run_lint",
        "codetrail_analyze_file",
    }
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
) -> tuple[tuple[str, ...], bool, ProtocolEvidence]:
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
                initialized = await session.initialize()
                listed_tools = await session.list_tools()
                names = tuple(tool.name for tool in listed_tools.tools)
                canonical_tools: list[Any] = []
                for tool in listed_tools.tools:
                    dump = getattr(tool, "model_dump", None)
                    if callable(dump):
                        canonical_tools.append(
                            dump(mode="json", by_alias=True, exclude_none=True)
                        )
                    else:  # pragma: no cover - older compatible SDK fallback
                        canonical_tools.append(str(tool))
                instructions = getattr(initialized, "instructions", None)
                instructions_text = instructions if isinstance(instructions, str) else ""
                evidence = ProtocolEvidence(
                    tools_digest=_json_digest(canonical_tools),
                    # Match scripts.mcp_catalog.text_digest exactly: MCP
                    # instructions identity is raw UTF-8, not JSON-quoted text.
                    instructions_digest=hashlib.sha256(
                        instructions_text.encode("utf-8")
                    ).hexdigest(),
                )
                result = await session.call_tool("list_dir", {"path": ".", "depth": 1})
                return names, bool(getattr(result, "isError", False)), evidence


def run_protocol_check(
    config: Mapping[str, Any],
    *,
    root: Path,
    env: Mapping[str, str],
    timeout: int,
) -> ProtocolEvidence:
    command = extract_codetrail_command(config, root=root)
    try:
        names, list_dir_error, evidence = asyncio.run(
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
            f"MCP 工具 contract 不符：預期 {len(EXPECTED_MCP_TOOLS)}、實得 {len(names)}；"
            + "；".join(details)
        )
    if names != EXPECTED_MCP_TOOL_ORDER:
        raise CanaryError(
            "MCP 工具順序不符 canonical PUBLIC_TOOL_ORDER；"
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
    env: Mapping[str, str],
    *,
    timeout: int = 5,
) -> dict[str, Any] | None:
    base_url = (env.get("AICODE_LLAMA_BASE_URL") or "http://localhost:8080").strip()
    url = _server_root_url(base_url) + "/props"
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


_FILE_PROMPT_REFERENCE_RE = re.compile(r"^\{file:(.+)\}$", re.DOTALL)


def _effective_agent_prompt_digest(config: Mapping[str, Any], agent_name: str) -> str:
    """Hash the prompt text one OpenCode agent will actually load.

    Managed prompts are file references.  Hashing only the reference would
    leave a stale canary cache when the file changes in place, while storing
    its content would violate the cache privacy contract.
    """
    agent = config.get("agent")
    entry = agent.get(agent_name) if isinstance(agent, Mapping) else None
    prompt = entry.get("prompt") if isinstance(entry, Mapping) else None
    if not isinstance(prompt, str):
        return _json_digest({"state": "missing-or-non-string"})
    match = _FILE_PROMPT_REFERENCE_RE.fullmatch(prompt.strip())
    if match is None:
        return _json_digest({"kind": "inline", "text": prompt})
    try:
        path = Path(match.group(1)).expanduser()
    except (OSError, ValueError):
        return _json_digest({"kind": "file", "state": "invalid"})
    return _json_digest(
        {
            "kind": "file",
            "content_digest": _file_digest(path),
        }
    )


def _effective_build_prompt_digest(config: Mapping[str, Any]) -> str:
    return _effective_agent_prompt_digest(config, "build")


def _compaction_agent_digest(config: Mapping[str, Any]) -> str:
    """Hash everything that decides how a compaction request is built.

    ``agent.compaction`` can override the compaction agent's model,
    temperature, options and system prompt (agent.ts).  A different summary
    engine produces a different conversation the model then reasons over, so
    a canary verdict recorded under one compaction agent must not be reused
    under another.  ``prompt`` is resolved through the same ``{file:...}``
    path as the build prompt: swapping the file's contents in place has to
    invalidate the cache.
    """
    agent = config.get("agent")
    entry = agent.get("compaction") if isinstance(agent, Mapping) else None
    if not isinstance(entry, Mapping):
        return _json_digest({"state": "absent"})
    return _json_digest(
        {
            "prompt": _effective_agent_prompt_digest(config, "compaction"),
            "model": entry.get("model"),
            "temperature": entry.get("temperature"),
            "options": entry.get("options"),
        }
    )


def _compaction_mode_digest(env: Mapping[str, str]) -> str:
    """Hash the compaction mode contract this run is operating under.

    Mode decides whether the compaction plugin is loaded, whether upstream
    auto-compaction is on, and what the retained tail looks like — all of
    which change what the model sees on the next turn.

    ``bound`` is not redundant with ``effective_config_hash``: the plugin
    refuses any ownership state that is not bound to the config actually in
    effect, so pointing ``OPENCODE_CONFIG`` at a byte-identical copy under a
    different path silently turns compaction off while every content hash
    stays the same.

    The plugin file and the canonical rule text are only hashed when a mode
    that loads them is active — editing them under ``native`` changes nothing
    about the run and must not cost a live canary.
    """
    # override 存在時,runtime plugin 讀的就是**那一份**:mode、bound 與要不要
    # 雜湊 plugin/rules 都要跟著它,否則 default state 是 native 而 override 是
    # codetrail 時,規則檔換內容仍會沿用舊 verdict。
    override = (env.get("AICODE_COMPACTION_STATE") or "").strip()
    if override:
        state_path = Path(override).expanduser()
    else:
        try:
            state_path = compaction_mode.state_path(env)
        except compaction_mode.CompactionModeError:
            state_path = None
    state = compaction_mode.load_state(path=state_path) if state_path else None
    mode = state.get("mode") if state else None
    payload: dict[str, Any] = {
        "mode": mode,
        "state_digest": state.get("digest") if state else None,
        "bound": _compaction_state_binding(state, env),
        # The private compaction eval points the runtime plugin at a different
        # ownership state.  A verdict recorded under that override describes a
        # different compaction contract than a normal run and must not be reused
        # — and two different overrides are two different contracts, so hash the
        # state they actually select rather than just "an override is set".
        "state_override": _compaction_override_digest(env),
    }
    if mode in compaction_mode.PLUGIN_MODES:
        payload["plugin"] = _file_digest(compaction_mode.PLUGIN_PATH)
        payload["rules"] = _file_digest(compaction_mode.RULES_DOC)
    return _json_digest(payload)


def _compaction_override_digest(env: Mapping[str, str]) -> str | None:
    """Identify the ownership state an ``AICODE_COMPACTION_STATE`` override selects."""
    raw = (env.get("AICODE_COMPACTION_STATE") or "").strip()
    if not raw:
        return None
    state = compaction_mode.load_state(path=Path(raw).expanduser())
    if state is None:
        return _json_digest({"path": _file_digest(Path(raw).expanduser()), "state": "rejected"})
    return _json_digest({"mode": state.get("mode"), "digest": state.get("digest")})


def _compaction_state_binding(
    state: Mapping[str, Any] | None, env: Mapping[str, str]
) -> str:
    """Does the recorded ownership state describe the config in effect here?"""
    if not state:
        return "no-state"
    try:
        config_path = resolve_config_path(dict(env))
    except Exception:  # noqa: BLE001 — 診斷用,不得讓 fingerprint 掛掉
        return "unresolved"
    if config_path is None:
        return "unresolved"
    return "bound" if compaction_mode.state_matches_config(state, config_path) else "foreign"


def build_fingerprint(
    *,
    root: Path,
    config: Mapping[str, Any],
    selected_model: str,
    props: Mapping[str, Any],
    opencode_version: str,
    env: Mapping[str, str],
    protocol_evidence: ProtocolEvidence | None = None,
) -> str:
    home_raw = (env.get("HOME") or env.get("USERPROFILE") or "").strip()
    home = Path(home_raw).expanduser() if home_raw else None
    tracked_files = {
        "canary": _file_digest(Path(__file__)),
        "mcp_server": _file_digest(REPO_ROOT / "mcp_server.py"),
        "project_agents": _file_digest(root / "AGENTS.md"),
        # lessons 注入檔也是模型看到的「專案規則」:內容變了要讓 model canary
        # 快取失效(aicode 會在 canary 之前先 render 好這個檔)。
        "project_lessons": _file_digest(root / ".codetrail" / "lessons.md"),
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
        "opencode_version": opencode_version,
        # Hash the resolved config inside the final digest.  This includes all
        # effective provider/MCP/agent settings without persisting credentials.
        "effective_config_hash": _json_digest(config),
        "effective_build_prompt_digest": _effective_build_prompt_digest(config),
        "compaction_agent_digest": _compaction_agent_digest(config),
        "compaction_mode_digest": _compaction_mode_digest(env),
        "live_tools_digest": protocol.tools_digest,
        "mcp_instructions_digest": protocol.instructions_digest,
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
        return "AICODE_TOOL_CANARY_TTL_SECONDS=0，快取已停用"
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


def inspect_implicit_events(output: str) -> ImplicitEvidence:
    """Classify only completed structured calls from the unnamed-intent probe."""
    session_ids: list[str] = []
    completed_read_only: list[tuple[str, dict[str, Any]]] = []
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
    )
    try:
        result = _run_process_with_heartbeat(command, root=root, env=env, timeout=timeout)
    except FileNotFoundError:
        return ModelEvidence(False, "找不到 opencode")
    except OSError as exc:
        return ModelEvidence(False, f"opencode run 無法啟動（{type(exc).__name__}）")
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


def _model_canary_command(
    *,
    root: Path,
    model_override: str,
    title: str,
    prompt: str,
) -> list[str]:
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
        title,
    ]
    if model_override:
        command.extend(("--model", model_override))
    command.append(prompt)
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
    )
    try:
        result = _run_process_with_heartbeat(command, root=root, env=env, timeout=timeout)
    except FileNotFoundError:
        return ImplicitEvidence(ImplicitStatus.FAIL)
    except OSError:
        return ImplicitEvidence(ImplicitStatus.FAIL)
    except subprocess.TimeoutExpired as exc:
        partial = inspect_implicit_events(_coerce_text(exc.stdout))
        return ImplicitEvidence(ImplicitStatus.TIMEOUT, partial.session_ids)
    if result.returncode != 0:
        failed = inspect_implicit_events(result.stdout)
        return ImplicitEvidence(ImplicitStatus.FAIL, failed.session_ids)
    return inspect_implicit_events(result.stdout)


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
        _print(
            "AICODE_TOOL_CANARY_WARN_ONLY 已不會略過 explicit/direct gate；"
            "只有 implicit routing 診斷不擋啟動",
            error=True,
        )
    _print(
        "已拒絕啟動；請修正 direct-tool / MCP / explicit tool-call 契約後重試",
        error=True,
    )
    return 2


def _report_implicit(status_value: ImplicitStatus, *, cached: bool) -> None:
    source = "cached" if cached else "live"
    if status_value is ImplicitStatus.OPTIMAL:
        _print(f"IMPLICIT {source} — status=optimal（自主選到根目錄列舉）")
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
        implicit_timeout = _env_int(
            env,
            "AICODE_TOOL_CANARY_IMPLICIT_TIMEOUT_SECONDS",
            DEFAULT_IMPLICIT_TIMEOUT_SECONDS,
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
        opencode_version = read_opencode_version(root, env)
        if opencode_version is None:
            raise CanaryError("opencode --version 無法讀取或輸出為空")
        try:
            require_direct_tool_contract(opencode_version, config)
        except DirectToolContractError as exc:
            raise CanaryError(str(exc)) from exc
        protocol_evidence = run_protocol_check(
            config, root=root, env=env, timeout=mcp_timeout
        )
    except CanaryError as exc:
        return _handle_failure(str(exc), warn_only=warn_only)

    if not isinstance(protocol_evidence, ProtocolEvidence):
        return _handle_failure(
            "MCP protocol check 沒有回傳 live tools/instructions fingerprint evidence",
            warn_only=warn_only,
        )
    _print(f"MCP PASS — {len(EXPECTED_MCP_TOOLS)} tools + list_dir round-trip")

    try:
        selected_model, model_override = _model_selection(
            config, explicit_model, frontend_args
        )
    except CanaryError as exc:
        return _handle_failure(str(exc), warn_only=warn_only)

    props = fetch_main_server_props(env)
    caps = props.get("chat_template_caps") if isinstance(props, Mapping) else None
    if isinstance(caps, Mapping) and caps.get("supports_tools") is False:
        return _handle_failure(
            "llama-server /props 明確回報 chat_template_caps.supports_tools=false；"
            "未執行任何 model canary",
            warn_only=warn_only,
        )
    cache_path = resolve_cache_path(env)
    cache_ready = props is not None and cache_path is not None
    fingerprint = ""
    bypass_cache = force or _truthy(env.get("AICODE_TOOL_CANARY_FORCE"))
    explicit_cached = False
    if cache_ready:
        assert props is not None
        fingerprint = build_fingerprint(
            root=root,
            config=config,
            selected_model=selected_model,
            props=props,
            opencode_version=opencode_version,
            env=env,
            protocol_evidence=protocol_evidence,
        )
        if bypass_cache:
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
        live_reason = "server /props、opencode 版本或快取路徑不可用；本次結果不會快取"

    if not explicit_cached:
        # 這一步是整個 aicode 啟動流程唯一會安靜跑數十秒以上的地方；先講清楚
        # 原因與預期時長，執行中再配合 heartbeat，避免被誤判成當機。
        _print(f"MODEL live canary — {live_reason}")
        _print(
            "現在實跑 explicit opencode run，驗證模型會真的呼叫 "
            "codetrail_list_dir；"
            f"單次上限 {model_timeout} 秒，執行中每 "
            f"{MODEL_CANARY_HEARTBEAT_SECONDS} 秒回報進度，不是當機。"
        )
        last_reason = "未知錯誤"
        explicit_passed = False
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
                            _print(
                                "WARNING — explicit PASS，但快取寫入失敗；下次會重測",
                                error=True,
                            )
                    _print(
                        f"MODEL PASS — structured codetrail_list_dir completed{suffix}"
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
            if attempt == 1:
                _print(f"MODEL RETRY — 第一次失敗：{last_reason}", error=True)

        if not explicit_passed:
            return _handle_failure(
                "MCP protocol 已通過，但 explicit 模型連續兩次未完成真實 tool call："
                + last_reason,
                warn_only=warn_only,
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
    if implicit.session_ids and not delete_canary_sessions(
        implicit.session_ids,
        root=root,
        env=env,
    ):
        _print("WARNING — 無法刪除本次 implicit canary session", error=True)
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
