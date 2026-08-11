#!/usr/bin/env python3
"""Helpers for resolving OpenCode TUI context against the main n_ctx.

OpenCode keeps the frontend chat budget in opencode.json as
provider.<key>.models.<model>.limit.context. CodeTrail's own MCP/native LLM
budget follows the main llama-server's real n_ctx. ``aicode`` transports that
single value as AICODE_N_CTX and safely synchronizes this mirror before opening
the TUI.
"""
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import n_ctx
from model_resolution import (
    has_external_provider_prefix,
    load_first_opencode_config,
    normalize_main_model,
    parse_cli_model_arg_detail,
)

DEFAULT_N_CTX = n_ctx.DEFAULT_N_CTX
# External compatibility; new code should use DEFAULT_N_CTX.
DEFAULT_DYNAMIC_CTX_MAX = DEFAULT_N_CTX


@dataclass(frozen=True)
class OpenCodeContextLimit:
    path: Path | None = None
    raw_model: str = ""
    model: str = ""
    provider_key: str = ""
    model_id: str = ""
    context: int | None = None
    error: str = ""
    present: bool = False

    @property
    def ok(self) -> bool:
        return self.context is not None and not self.error


def n_ctx_from_env(env: Mapping[str, str] | None = None) -> int:
    environ = env if env is not None else os.environ
    return n_ctx.resolve_n_ctx(environ).value


def dynamic_ctx_max_from_env(env: Mapping[str, str] | None = None) -> int:
    """Deprecated compatibility alias for callers outside this repository."""
    return n_ctx_from_env(env)


def _provider_model_from_raw(raw: str) -> tuple[str, str] | None:
    value = raw.strip()
    if "/" not in value:
        return None
    if value.startswith(("/", "~", "./", "../")):
        return None
    if value.lower().endswith(".gguf"):
        return None
    if has_external_provider_prefix(value):
        return None
    provider_key, model_id = value.split("/", 1)
    if not provider_key or not model_id:
        return None
    return provider_key, model_id


def _limit_context(spec: object) -> int | None:
    if not isinstance(spec, dict):
        return None
    limit = spec.get("limit")
    if not isinstance(limit, dict):
        return None
    ctx = limit.get("context")
    if isinstance(ctx, bool):
        return None
    if isinstance(ctx, int):
        return ctx
    if isinstance(ctx, str) and ctx.isdigit():
        return int(ctx)
    return None


def _scan_matching_limits(
    providers: object,
    *,
    raw_model: str,
    model: str,
) -> list[OpenCodeContextLimit]:
    if not isinstance(providers, dict):
        return []

    matches: list[OpenCodeContextLimit] = []
    for provider_key, provider_spec in providers.items():
        if not isinstance(provider_spec, dict):
            continue
        models = provider_spec.get("models")
        if not isinstance(models, dict):
            continue
        for model_id, spec in models.items():
            if not isinstance(model_id, str) or not isinstance(spec, dict):
                continue
            names = {model_id}
            name = spec.get("name")
            if isinstance(name, str):
                names.add(name)
            if raw_model in names or model in names:
                matches.append(
                    OpenCodeContextLimit(
                        raw_model=raw_model,
                        model=model,
                        provider_key=str(provider_key),
                        model_id=model_id,
                        context=_limit_context(spec),
                        present=True,
                    )
                )
    return matches


def resolve_active_opencode_context_limit(
    env: Mapping[str, str] | None = None,
    argv: Sequence[str] | None = None,
) -> OpenCodeContextLimit:
    """Return limit.context for the OpenCode model that will be active.

    CLI -m/--model wins for model identity because aicode forwards it to
    OpenCode. Without CLI, opencode.json's top-level "model" is the active
    model. Missing config or missing limit.context is not an error here; callers
    decide whether to warn or skip.
    """
    environ = env if env is not None else os.environ
    args = list(argv or [])

    cli_arg = parse_cli_model_arg_detail(args)
    if cli_arg.error:
        return OpenCodeContextLimit(error=cli_arg.error, present=True)

    path, data, error = load_first_opencode_config(environ)
    if error:
        return OpenCodeContextLimit(path=path, error=error, present=True)
    if not data:
        return OpenCodeContextLimit(path=path, present=False)

    raw_model = cli_arg.values[-1] if cli_arg.values else data.get("model")
    if not isinstance(raw_model, str) or not raw_model.strip():
        return OpenCodeContextLimit(path=path, error='OpenCode config missing "model"', present=True)
    raw_model = raw_model.strip()

    model_res = normalize_main_model(raw_model, "OpenCode model", path=path)
    if model_res.error:
        return OpenCodeContextLimit(path=path, raw_model=raw_model, error=model_res.error, present=True)
    model = model_res.model

    providers = data.get("provider")
    provider_pair = _provider_model_from_raw(raw_model)
    if provider_pair and isinstance(providers, dict):
        provider_key, model_id = provider_pair
        provider_spec = providers.get(provider_key)
        if isinstance(provider_spec, dict):
            models = provider_spec.get("models")
            if isinstance(models, dict):
                spec = models.get(model_id)
                if isinstance(spec, dict):
                    return OpenCodeContextLimit(
                        path=path,
                        raw_model=raw_model,
                        model=model,
                        provider_key=provider_key,
                        model_id=model_id,
                        context=_limit_context(spec),
                        present=True,
                    )

    matches = _scan_matching_limits(providers, raw_model=raw_model, model=model)
    for idx, match in enumerate(matches):
        matches[idx] = OpenCodeContextLimit(
            path=path,
            raw_model=match.raw_model,
            model=match.model,
            provider_key=match.provider_key,
            model_id=match.model_id,
            context=match.context,
            present=match.present,
        )

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return OpenCodeContextLimit(
            path=path,
            raw_model=raw_model,
            model=model,
            error=(
                "multiple matching OpenCode model entries; use an explicit "
                "provider/model id so an automatic update is unambiguous"
            ),
            present=True,
        )

    return OpenCodeContextLimit(
        path=path,
        raw_model=raw_model,
        model=model,
        present=True,
    )
