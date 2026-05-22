#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared main-model resolution helpers.

This module intentionally has no dependency on config.py. It is used by
config.py, scripts/resolve_main_model.py, and scripts/doctor.py, including
before the aicode wrapper has exported AICODE_MODEL.

CodeTrail 只跑 llama.cpp llama-server。AICODE_MODEL 可以是:
  - registry 裡登記的 bare name(例如 "qwen3-coder-30b")
  - GGUF 絕對路徑(例如 "/models/qwen3-coder-30b-q4_k_m.gguf")
opencode.json `model` 欄位若是 "<provider>/<name>" 形式 (例如 OpenAI 留下的舊
設定 "openai/gpt-4o"),會被視為非本機 provider 拒絕 — 因為我們不打外部 API。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Mapping, Sequence


# 留下的這份「非本機 provider」清單是給 opencode.json 設定錯誤時用的:
# 使用者可能還沿用以前 ollama/* 或 openai/* 那種寫法,讓 resolution 報得明確一點。
EXTERNAL_PROVIDER_PREFIXES = (
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
    "ollama/",   # 舊的 ollama provider 寫法,現在不接受
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


def has_external_provider_prefix(value: str) -> bool:
    """value 開頭是不是 openai/ ollama/ anthropic/ 之類的 provider prefix。"""
    lowered = value.lower()
    return any(lowered.startswith(prefix) for prefix in EXTERNAL_PROVIDER_PREFIXES)


def _looks_like_path(value: str) -> bool:
    """value 是不是路徑(絕對 / ~ / 結尾 .gguf)。"""
    if not value:
        return False
    if value.startswith(("/", "~")):
        return True
    if value.startswith("./") or value.startswith("../"):
        return True
    if value.lower().endswith(".gguf"):
        return True
    return False


def normalize_main_model(
    raw: str | None,
    source: str,
    *,
    path: Path | None = None,
) -> ModelResolution:
    """Validate a candidate main-model identifier.

    可接受的形式:
      1. bare name           "qwen3-coder-30b"          → model = "qwen3-coder-30b"
      2. GGUF 絕對 / 相對路徑  "/models/foo.gguf" / "~/m.gguf"
      3. custom-provider 形式 "myprovider/qwen3-coder-30b"  (OpenCode openai-compat
         provider 設定下會這樣寫 model:) → 自動 strip 成 "qwen3-coder-30b"

    拒絕:
      - placeholder ('<...>')
      - 已知 external provider prefix (openai/、anthropic/、ollama/ 等),因為這代表
        該 model 是要打外部 API,跟我們的 llama-server 設計不相容。
    """
    value = (raw or "").strip()
    if not value:
        return ModelResolution(source=source, raw=value, path=path, present=False)

    if is_placeholder_model(value):
        return ModelResolution(
            source=source,
            raw=value,
            path=path,
            present=True,
            error=f"{source} is a placeholder; replace it with a real model name or GGUF path.",
        )

    if has_external_provider_prefix(value):
        return ModelResolution(
            source=source,
            raw=value,
            path=path,
            present=True,
            error=(
                f"{source} 帶外部 provider prefix {value!r};CodeTrail 只跑本地"
                " llama-server,請改寫 bare name 或 GGUF 路徑 (例: \"qwen3-coder-30b\")"
            ),
        )

    # 路徑形式:整段保留(讓 caller 用 resolve_model_path 解析)
    if _looks_like_path(value):
        return ModelResolution(model=value, source=source, raw=value, path=path, present=True)

    # `<custom-provider>/<bare>` 形式:strip 第一段
    if "/" in value:
        bare = value.split("/", 1)[1].strip()
        if not bare or is_placeholder_model(bare):
            return ModelResolution(
                source=source,
                raw=value,
                path=path,
                present=True,
                error=f"{source} provider 後面的 model name 是空的或 placeholder",
            )
        return ModelResolution(model=bare, source=source, raw=value, path=path, present=True)

    return ModelResolution(model=value, source=source, raw=value, path=path, present=True)


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
