#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared main-model resolution helpers.

This module intentionally has no dependency on config.py. It is used by
config.py, scripts/resolve_main_model.py, and scripts/doctor.py, including
before the aicode wrapper has exported AICODE_MODEL.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Mapping, Sequence


NON_OLLAMA_PROVIDER_PREFIXES = (
    "openai/",
    "anthropic/",
    "vertex/",
    "google/",
    "cohere/",
    "groq/",
    "azure/",
    "bedrock/",
    "aws/",
    "deepseek/",
    "mistral/",
    "openrouter/",
    "together/",
    "perplexity/",
    "mistral.ai/",
    "xai/",
)


@dataclass(frozen=True)
class ModelResolution:
    model: str = ""
    source: str = ""
    raw: str = ""
    error: str = ""
    path: Path | None = None
    present: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.model) and not self.error


@dataclass(frozen=True)
class CliModelArg:
    values: tuple[str, ...] = ()
    error: str = ""

    @property
    def present(self) -> bool:
        return bool(self.values) or bool(self.error)

    @property
    def raw(self) -> str:
        return self.values[0] if self.values else ""


def is_placeholder_model(value: str) -> bool:
    if not value:
        return True
    return "<" in value or ">" in value


def strip_ollama_prefix(value: str) -> str:
    if value.startswith("ollama/"):
        return value[len("ollama/") :]
    return value


def has_non_ollama_provider_prefix(value: str) -> bool:
    lowered = value.lower()
    return any(lowered.startswith(prefix) for prefix in NON_OLLAMA_PROVIDER_PREFIXES)


def normalize_main_model(
    raw: str | None,
    source: str,
    *,
    require_ollama_prefix: bool = False,
    path: Path | None = None,
) -> ModelResolution:
    value = (raw or "").strip()
    if not value:
        return ModelResolution(source=source, raw=value, path=path, present=False)

    if is_placeholder_model(value):
        return ModelResolution(
            source=source,
            raw=value,
            path=path,
            present=True,
            error=f"{source} is a placeholder; replace it with a real Ollama model name.",
        )

    if require_ollama_prefix and not value.startswith("ollama/"):
        return ModelResolution(
            source=source,
            raw=value,
            path=path,
            present=True,
            error=f'{source} must be "ollama/<MODEL>" for CodeTrail.',
        )

    bare = strip_ollama_prefix(value)
    if not bare or is_placeholder_model(bare):
        return ModelResolution(
            source=source,
            raw=value,
            path=path,
            present=True,
            error=f"{source} does not contain a real model name.",
        )

    if has_non_ollama_provider_prefix(bare):
        return ModelResolution(
            source=source,
            raw=value,
            path=path,
            present=True,
            error=(
                f"{source} points at non-Ollama provider {bare!r}; "
                "CodeTrail MCP only uses Ollama native APIs."
            ),
        )

    return ModelResolution(model=bare, source=source, raw=value, path=path, present=True)


def parse_cli_model_arg_detail(argv: Sequence[str]) -> CliModelArg:
    values: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "-m" or arg == "--model":
            if i + 1 >= len(argv):
                return CliModelArg(
                    values=tuple(values),
                    error=f"{arg} requires a model value.",
                )
            value = str(argv[i + 1]).strip()
            if not value or value.startswith("-"):
                return CliModelArg(
                    values=tuple(values),
                    error=f"{arg} requires a model value.",
                )
            values.append(value)
            i += 2
            continue
        if arg.startswith("-m="):
            value = arg[3:].strip()
            if not value:
                return CliModelArg(values=tuple(values), error="-m requires a model value.")
            values.append(value)
            i += 1
            continue
        if arg.startswith("--model="):
            value = arg[len("--model=") :].strip()
            if not value:
                return CliModelArg(
                    values=tuple(values),
                    error="--model requires a model value.",
                )
            values.append(value)
            i += 1
            continue
        i += 1
    return CliModelArg(values=tuple(values))


def parse_cli_model_arg(argv: Sequence[str]) -> str:
    return parse_cli_model_arg_detail(argv).raw


def opencode_config_candidates(env: Mapping[str, str] | None = None) -> list[Path]:
    environ = env if env is not None else os.environ
    explicit = (environ.get("OPENCODE_CONFIG") or "").strip()
    if explicit:
        return [Path(explicit).expanduser()]

    home = environ.get("HOME") or environ.get("USERPROFILE")
    if home:
        return [Path(home) / ".config" / "opencode" / "opencode.json"]

    return []


def load_first_opencode_config(
    env: Mapping[str, str] | None = None,
) -> tuple[Path | None, dict | None, str]:
    environ = env if env is not None else os.environ
    explicit = bool((environ.get("OPENCODE_CONFIG") or "").strip())
    for path in opencode_config_candidates(environ):
        try:
            if not path.is_file():
                if explicit:
                    return path, None, "OPENCODE_CONFIG file does not exist."
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return path, None, str(exc)
        if not isinstance(data, dict):
            return path, None, "OpenCode config root must be a JSON object."
        return path, data, ""
    return None, None, ""


def resolve_opencode_main_model(
    env: Mapping[str, str] | None = None,
) -> ModelResolution:
    path, data, error = load_first_opencode_config(env)
    if error:
        return ModelResolution(source="opencode.json", path=path, present=True, error=error)
    if not data:
        return ModelResolution(source="opencode.json", path=path, present=False)

    raw = data.get("model")
    if not isinstance(raw, str):
        return ModelResolution(source="opencode.json", path=path, present=False)

    return normalize_main_model(
        raw,
        "opencode.json model",
        require_ollama_prefix=True,
        path=path,
    )


def resolve_main_model_from_env(
    env: Mapping[str, str] | None = None,
) -> ModelResolution:
    environ = env if env is not None else os.environ
    raw = (environ.get("AICODE_MODEL") or "").strip()
    if raw:
        return normalize_main_model(raw, "AICODE_MODEL")
    return resolve_opencode_main_model(environ)
