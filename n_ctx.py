#!/usr/bin/env python3
"""Resolve the one main-model n_ctx value used by CodeTrail.

Normal users set the value once through ``set_config.sh --ctx``.  At runtime
``aicode`` observes the main llama-server and transports that value through
``AICODE_N_CTX`` so CodeTrail and OpenCode can follow it.  The old
``AICODE_DYNAMIC_NUM_CTX_MAX`` name remains a read-only compatibility alias;
it is deliberately not the primary setting because it exposes an internal
implementation detail as a second user-facing "maximum".
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

N_CTX_ENV = "AICODE_N_CTX"
LEGACY_DYNAMIC_MAX_ENV = "AICODE_DYNAMIC_NUM_CTX_MAX"
LEGACY_NUM_CTX_ENV = "AICODE_NUM_CTX"
DEFAULT_N_CTX = 65_536
MAX_N_CTX = 1_048_576


@dataclass(frozen=True)
class NContextResolution:
    value: int
    source: str
    legacy: bool = False


def _parse_positive_int(raw: str, name: str) -> int:
    value = raw.strip()
    if not value.isdecimal():
        raise ValueError(f"{name}={raw!r} 必須是正整數")
    parsed = int(value)
    if not 1 <= parsed <= MAX_N_CTX:
        raise ValueError(f"{name} 必須介於 1..{MAX_N_CTX}")
    return parsed


def resolve_n_ctx(
    env: Mapping[str, str],
    *,
    default: int = DEFAULT_N_CTX,
    default_source: str = "default",
) -> NContextResolution:
    """Resolve main n_ctx with one canonical env and one legacy alias.

    ``AICODE_NUM_CTX`` is intentionally not a fallback.  Dynamic mode has
    ignored it for years, and suddenly treating it as the server budget could
    raise a 64K deployment to an unsafe stale 128K shell value.
    """
    canonical = (env.get(N_CTX_ENV) or "").strip()
    if canonical:
        return NContextResolution(
            _parse_positive_int(canonical, N_CTX_ENV),
            N_CTX_ENV,
        )

    legacy = (env.get(LEGACY_DYNAMIC_MAX_ENV) or "").strip()
    if legacy:
        return NContextResolution(
            _parse_positive_int(legacy, LEGACY_DYNAMIC_MAX_ENV),
            LEGACY_DYNAMIC_MAX_ENV,
            legacy=True,
        )

    if not 1 <= int(default) <= MAX_N_CTX:
        raise ValueError(f"default n_ctx 必須介於 1..{MAX_N_CTX}")
    return NContextResolution(int(default), default_source)
