#!/usr/bin/env python3
"""Live MCP catalog capture and routing-cost helpers.

The routing evaluation must measure the contract a client actually receives.
This module therefore consumes ``FastMCP.list_tools()`` or a real stdio MCP
``initialize``/``tools/list`` round trip; it never scrapes Python source.

Only aggregate counts and digests are intended for persisted evaluation
artifacts.  Descriptions and schemas remain in memory long enough to run an
evaluation, but callers should use :meth:`CatalogSnapshot.summary` when
serialising results.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# This deliberately keeps json.dumps' default separators.  It reproduces the
# reviewed pre-change baseline (33,299 chars) and is part of the eval contract.
SCHEMA_JSON_RULE = "json.dumps(value, ensure_ascii=False, sort_keys=True); len() counts Unicode code points"

# Digests use a compact but otherwise canonical representation.  Digest input
# is never persisted, so this avoids confusing a byte identity with the schema
# character-budget convention above.
DIGEST_JSON_RULE = 'json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))'

MINIMAL_TOKEN_MESSAGE: tuple[dict[str, str], ...] = ({"role": "user", "content": "."},)

# The "effective" character count (descriptions + input schemas) is the number a
# client actually pays for.  Frozen eval baselines were recorded under the old
# key name and are measured data, so readers accept both spellings and writers
# only ever emit the current one.  See :func:`effective_chars`.
EFFECTIVE_CHARS_KEY = "catalog_effective_chars"
LEGACY_EFFECTIVE_CHARS_KEY = "opencode_effective_chars"


class CatalogError(RuntimeError):
    """A live catalog could not be acquired or did not have the MCP shape."""


class TokenMeasurementError(RuntimeError):
    """The catalog token delta could not be measured without guessing."""


@dataclass(frozen=True)
class StdioMcpCommand:
    """The stdio command that starts one MCP server, plus its extra environment."""

    argv: tuple[str, ...]
    environment: dict[str, str]


@dataclass(frozen=True)
class CatalogTool:
    """Normalised model-visible subset of one MCP Tool."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    raw: dict[str, Any]

    def openai_tool(self, *, name_prefix: str = "codetrail_") -> dict[str, Any]:
        """Return the function-tool shape the client sends to llama-server."""

        return {
            "type": "function",
            "function": {
                "name": f"{name_prefix}{self.name}",
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass(frozen=True)
class ToolCatalogCount:
    name: str
    description_chars: int
    input_schema_chars: int
    output_schema_chars: int

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description_chars": self.description_chars,
            "input_schema_chars": self.input_schema_chars,
            "output_schema_chars": self.output_schema_chars,
        }


@dataclass(frozen=True)
class CatalogSnapshot:
    """A live ``tools/list`` response plus privacy-safe aggregate metrics."""

    source: str
    tools: tuple[CatalogTool, ...]
    instructions: str
    per_tool: tuple[ToolCatalogCount, ...]
    description_chars: int
    input_schema_chars: int
    output_schema_chars: int
    catalog_chars: int
    #: 「模型實際看得到的字元數」= description + input schema。凍結的歷史 baseline
    #: 用舊鍵名記錄同一個數字(資料檔不重造),讀取一律走 :func:`effective_chars`。
    catalog_effective_chars: int
    tools_digest: str
    instructions_digest: str
    canonical_tools_list_chars: int

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.tools)

    def summary(self, *, include_per_tool: bool = False) -> dict[str, Any]:
        """Return counts/digests only; never descriptions or schemas."""

        data: dict[str, Any] = {
            "measurement_source": self.source,
            "schema_json_rule": SCHEMA_JSON_RULE,
            "tool_count": len(self.tools),
            "description_chars": self.description_chars,
            "input_schema_chars": self.input_schema_chars,
            "output_schema_chars": self.output_schema_chars,
            "catalog_chars": self.catalog_chars,
            EFFECTIVE_CHARS_KEY: self.catalog_effective_chars,
            "instructions_chars": len(self.instructions),
            "tools_digest": self.tools_digest,
            "instructions_digest": self.instructions_digest,
            "canonical_tools_list_chars": self.canonical_tools_list_chars,
        }
        if include_per_tool:
            data["per_tool"] = [item.summary() for item in self.per_tool]
        return data


@dataclass(frozen=True)
class CatalogTokenMeasurement:
    """Tool A/B cost plus the separately measured MCP-instructions delta."""

    method: str
    no_tools_prompt_tokens: int
    tools_prompt_tokens: int
    instructions_prompt_tokens: int
    catalog_prompt_tokens: int

    def summary(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "no_tools_prompt_tokens": self.no_tools_prompt_tokens,
            "tools_prompt_tokens": self.tools_prompt_tokens,
            "instructions_prompt_tokens": self.instructions_prompt_tokens,
            "catalog_prompt_tokens": self.catalog_prompt_tokens,
        }


def effective_chars(summary: Mapping[str, Any]) -> int:
    """Read the effective character count from a catalog summary of either era.

    The current name wins; the frozen historical baselines only carry the legacy
    one.  A summary that has neither is an error rather than a zero: comparing
    two silently defaulted zeros would report "the frozen contract still holds"
    for a measurement that was never taken.
    """

    for key in (EFFECTIVE_CHARS_KEY, LEGACY_EFFECTIVE_CHARS_KEY):
        if key in summary:
            value = summary[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise CatalogError(f"{key} must be an integer, got {type(value).__name__}")
            return value
    raise CatalogError(
        f"catalog summary has no {EFFECTIVE_CHARS_KEY} (or {LEGACY_EFFECTIVE_CHARS_KEY})"
    )


def schema_json(value: object) -> str:
    """Canonical schema text used for all character-budget measurements."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def compact_canonical_json(value: object) -> str:
    """Canonical JSON used only for content identity digests."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def json_digest(value: object) -> str:
    return hashlib.sha256(compact_canonical_json(value).encode("utf-8")).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _model_dump(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        dumped = dumper(by_alias=True, exclude_none=True)
        if isinstance(dumped, Mapping):
            return dict(dumped)
    legacy_dumper = getattr(value, "dict", None)
    if callable(legacy_dumper):
        dumped = legacy_dumper(by_alias=True, exclude_none=True)
        if isinstance(dumped, Mapping):
            return dict(dumped)
    raise CatalogError(f"tools/list item is not serialisable: {type(value).__name__}")


def _normalise_schema(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CatalogError(f"{label} must be a JSON object")
    return dict(value)


def _normalise_tool(value: object) -> CatalogTool:
    raw = _model_dump(value)
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise CatalogError("tools/list item has no non-empty name")
    description = raw.get("description", "")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise CatalogError(f"tool {name!r} description is not a string")

    input_value = raw.get("inputSchema", raw.get("input_schema"))
    input_schema = _normalise_schema(input_value, label=f"{name}.inputSchema")

    output_value = raw.get("outputSchema", raw.get("output_schema"))
    output_schema = None
    if output_value is not None:
        output_schema = _normalise_schema(output_value, label=f"{name}.outputSchema")

    # Rebuild aliases so digests do not depend on a Pydantic object's internal
    # snake_case spelling.  Preserve protocol metadata if it was present.
    canonical_raw = dict(raw)
    canonical_raw.pop("input_schema", None)
    canonical_raw.pop("output_schema", None)
    canonical_raw["name"] = name
    canonical_raw["description"] = description
    canonical_raw["inputSchema"] = input_schema
    if output_schema is None:
        canonical_raw.pop("outputSchema", None)
    else:
        canonical_raw["outputSchema"] = output_schema

    return CatalogTool(name, description, input_schema, output_schema, canonical_raw)


def measure_catalog(
    tools: Sequence[object],
    *,
    instructions: str = "",
    source: str,
) -> CatalogSnapshot:
    """Measure a live tool sequence using the frozen canonical-JSON rule."""

    if not isinstance(instructions, str):
        raise CatalogError("MCP initialize instructions must be a string")
    normalised = tuple(_normalise_tool(tool) for tool in tools)
    if not normalised:
        raise CatalogError("tools/list returned zero tools")
    names = [tool.name for tool in normalised]
    if len(names) != len(set(names)):
        duplicates = sorted(name for name in set(names) if names.count(name) > 1)
        raise CatalogError(f"tools/list contains duplicate names: {duplicates}")

    per_tool = tuple(
        ToolCatalogCount(
            name=tool.name,
            description_chars=len(tool.description),
            input_schema_chars=len(schema_json(tool.input_schema)),
            output_schema_chars=(
                len(schema_json(tool.output_schema)) if tool.output_schema is not None else 0
            ),
        )
        for tool in normalised
    )
    description_chars = sum(item.description_chars for item in per_tool)
    input_schema_chars = sum(item.input_schema_chars for item in per_tool)
    output_schema_chars = sum(item.output_schema_chars for item in per_tool)
    canonical_tools = [tool.raw for tool in normalised]

    return CatalogSnapshot(
        source=source,
        tools=normalised,
        instructions=instructions,
        per_tool=per_tool,
        description_chars=description_chars,
        input_schema_chars=input_schema_chars,
        output_schema_chars=output_schema_chars,
        catalog_chars=description_chars + input_schema_chars + output_schema_chars,
        catalog_effective_chars=description_chars + input_schema_chars,
        tools_digest=json_digest(canonical_tools),
        instructions_digest=text_digest(instructions),
        canonical_tools_list_chars=len(compact_canonical_json(canonical_tools)),
    )


async def catalog_from_fastmcp(server: object) -> CatalogSnapshot:
    """Acquire a catalog from an in-process FastMCP instance (CI path)."""

    list_tools = getattr(server, "list_tools", None)
    if not callable(list_tools):
        raise CatalogError("in-process object has no callable list_tools()")
    listed = list_tools()
    if inspect.isawaitable(listed):
        listed = await listed
    tools = getattr(listed, "tools", listed)
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
        raise CatalogError("in-process list_tools() did not return a tool sequence")

    instructions = getattr(server, "instructions", None)
    if instructions is None:
        instructions = getattr(getattr(server, "_mcp_server", None), "instructions", "")
    return measure_catalog(
        tools,
        instructions=instructions or "",
        source="in_process_fastmcp_list_tools",
    )


async def catalog_from_stdio(
    command: StdioMcpCommand,
    *,
    root: Path,
    environment: Mapping[str, str] | None = None,
    timeout_seconds: int = 120,
) -> CatalogSnapshot:
    """Run real MCP initialize/tools/list against an effective stdio command."""

    if timeout_seconds < 1:
        raise CatalogError("stdio catalog timeout must be positive")
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:  # pragma: no cover - depends on optional runtime
        raise CatalogError("the mcp package is required for stdio catalog capture") from exc

    root = root.resolve()
    # root 走 **argv**,不走環境變數:同一台機器上的殼層可能留著別份安裝的
    # `AICODE_ROOT`,而它會決定 server 的沙箱邊界。`child_env()` 剝掉那一整組
    # 前綴,`--root` 明確附在命令列上。
    #
    # **剝除在最後**:呼叫端傳進來的 `environment` / `command.environment` 有可能
    # 本身就是一份未剝除的 `os.environ`,先套再剝才不會把剛拿掉的東西加回去。
    import client_mcp

    server_env = client_mcp.child_env()
    if environment:
        server_env.update(environment)
    server_env.update(command.environment)
    for key in list(server_env):
        if key.startswith(client_mcp.STRIPPED_ENV_PREFIXES):
            server_env.pop(key, None)

    args = list(command.argv[1:])
    if "--root" not in args:
        args.extend(["--root", str(root)])

    params = StdioServerParameters(
        command=command.argv[0],
        args=args,
        env=server_env,
        cwd=str(root),
    )

    async def _roundtrip() -> CatalogSnapshot:
        # Startup logs may contain project-local paths.  Keep them in an
        # anonymous file and never return or persist them.
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    initialised = await session.initialize()
                    listed = await session.list_tools()
                    instructions = getattr(initialised, "instructions", None) or ""
                    return measure_catalog(
                        listed.tools,
                        instructions=instructions,
                        source="stdio_initialize_tools_list",
                    )

    try:
        return await asyncio.wait_for(_roundtrip(), timeout=timeout_seconds)
    except TimeoutError as exc:
        raise CatalogError(f"MCP initialize/tools/list exceeded {timeout_seconds} seconds") from exc
    except CatalogError:
        raise
    except Exception as exc:
        raise CatalogError(f"MCP initialize/tools/list failed ({type(exc).__name__})") from exc


def build_chat_probe_payload(
    *,
    model: str,
    tools: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Build one side of the frozen same-message, one-token A/B probe."""

    payload: dict[str, Any] = {
        "model": model,
        "messages": [dict(message) for message in MINIMAL_TOKEN_MESSAGE],
        "max_tokens": 1,
        "stream": False,
    }
    if tools is not None:
        payload["tools"] = [dict(tool) for tool in tools]
    return payload


def _usage_prompt_tokens(response: Mapping[str, Any]) -> int | None:
    usage = response.get("usage")
    value = usage.get("prompt_tokens") if isinstance(usage, Mapping) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _template_prompt(response: Mapping[str, Any]) -> str:
    for key in ("prompt", "content"):
        value = response.get(key)
        if isinstance(value, str):
            return value
    raise TokenMeasurementError("/apply-template response has no string prompt")


def _token_count(response: Mapping[str, Any]) -> int:
    tokens = response.get("tokens")
    if isinstance(tokens, list):
        return len(tokens)
    count = response.get("count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return count
    raise TokenMeasurementError("/tokenize response has no token list or count")


def _apply_template_payload(probe: Mapping[str, Any]) -> dict[str, Any]:
    """Project a chat probe onto llama.cpp's non-generation endpoint schema."""

    payload: dict[str, Any] = {
        "messages": probe["messages"],
        "add_generation_prompt": True,
    }
    if "tools" in probe:
        payload["tools"] = probe["tools"]
    return payload


def _token_measurement(
    method: str,
    no_tools_prompt_tokens: int,
    tools_prompt_tokens: int,
    instructions_prompt_tokens: int,
) -> CatalogTokenMeasurement:
    if tools_prompt_tokens < no_tools_prompt_tokens:
        raise TokenMeasurementError("tools prompt token count is smaller than no-tools")
    if instructions_prompt_tokens < 0:
        raise TokenMeasurementError("instructions prompt token count is negative")
    return CatalogTokenMeasurement(
        method=method,
        no_tools_prompt_tokens=no_tools_prompt_tokens,
        tools_prompt_tokens=tools_prompt_tokens,
        instructions_prompt_tokens=instructions_prompt_tokens,
        catalog_prompt_tokens=(tools_prompt_tokens - no_tools_prompt_tokens + instructions_prompt_tokens),
    )


def _measure_instructions_prompt_tokens(
    instructions: str,
    *,
    tools: Sequence[Mapping[str, Any]],
    post_json: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
) -> int:
    """Measure MCP instructions separately without changing the tools A/B."""

    if not instructions:
        return 0
    plain_payload = {
        "messages": [dict(message) for message in MINIMAL_TOKEN_MESSAGE],
        "tools": [dict(tool) for tool in tools],
        "add_generation_prompt": True,
    }
    instructed_payload = {
        "messages": [
            {"role": "system", "content": instructions},
            *(dict(message) for message in MINIMAL_TOKEN_MESSAGE),
        ],
        "tools": [dict(tool) for tool in tools],
        "add_generation_prompt": True,
    }
    plain_template = post_json("/apply-template", plain_payload)
    instructed_template = post_json("/apply-template", instructed_payload)
    plain_tokens = _token_count(
        post_json(
            "/tokenize",
            {"content": _template_prompt(plain_template), "add_special": True},
        )
    )
    instructed_tokens = _token_count(
        post_json(
            "/tokenize",
            {"content": _template_prompt(instructed_template), "add_special": True},
        )
    )
    if instructed_tokens < plain_tokens:
        raise TokenMeasurementError("instructions prompt token count is smaller than plain prompt")
    return instructed_tokens - plain_tokens


def measure_catalog_prompt_tokens(
    *,
    model: str,
    catalog: CatalogSnapshot,
    post_json: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    name_prefix: str = "codetrail_",
) -> CatalogTokenMeasurement:
    """Measure catalog tokens, preferring ``usage.prompt_tokens``.

    ``post_json`` is injected so deterministic tests can prove request equality
    without network access.  A live caller should route paths to the same
    llama-server and use a no-proxy/no-redirect HTTP client.
    """

    openai_tools = [tool.openai_tool(name_prefix=name_prefix) for tool in catalog.tools]
    without_payload = build_chat_probe_payload(model=model, tools=None)
    with_payload = build_chat_probe_payload(model=model, tools=openai_tools)
    without_response = post_json("/v1/chat/completions", without_payload)
    with_response = post_json("/v1/chat/completions", with_payload)

    without_usage = _usage_prompt_tokens(without_response)
    with_usage = _usage_prompt_tokens(with_response)
    if without_usage is not None and with_usage is not None:
        method = "usage.prompt_tokens"
        without_tokens = without_usage
        with_tokens = with_usage
    else:
        # Missing usage on either arm invalidates an asymmetric comparison.
        # Apply the exact same two request payloads through llama.cpp's
        # template/tokenizer endpoints instead.
        without_template = post_json("/apply-template", _apply_template_payload(without_payload))
        with_template = post_json("/apply-template", _apply_template_payload(with_payload))
        without_tokens = _token_count(
            post_json(
                "/tokenize",
                {"content": _template_prompt(without_template), "add_special": True},
            )
        )
        with_tokens = _token_count(
            post_json(
                "/tokenize",
                {"content": _template_prompt(with_template), "add_special": True},
            )
        )
        method = "apply-template+tokenize"

    instructions_tokens = _measure_instructions_prompt_tokens(
        catalog.instructions,
        tools=openai_tools,
        post_json=post_json,
    )
    if catalog.instructions:
        method += "+instructions(apply-template+tokenize)"
    return _token_measurement(
        method,
        without_tokens,
        with_tokens,
        instructions_tokens,
    )


def assert_public_tool_contract(snapshot: CatalogSnapshot) -> None:
    """Validate order against ``mcp_contract`` without duplicating the list."""

    try:
        from mcp_contract import PUBLIC_TOOL_ORDER
    except ImportError as exc:
        raise CatalogError("mcp_contract.PUBLIC_TOOL_ORDER is unavailable") from exc
    if snapshot.tool_names != tuple(PUBLIC_TOOL_ORDER):
        raise CatalogError("live tools/list names or order differ from mcp_contract.PUBLIC_TOOL_ORDER")
