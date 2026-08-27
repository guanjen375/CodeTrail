#!/usr/bin/env python3
"""Canonical CodeTrail build prompt and OpenCode config merge contract.

The installable prompt lives in a single fenced block in
``docs/opencode-build-prompt.md``.  Keeping extraction and config merging here
gives ``set_config`` and the launch-time contract checker one implementation of
the user-customisation rules.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_PROMPT_DOC = REPO_ROOT / "docs" / "opencode-build-prompt.md"
BUILD_PROMPT_MAX_CHARS = 1500
BUILD_PROMPT_RELATIVE_PATH = Path(
    ".config/codetrail/opencode-build-prompt.md"
)

_PROMPT_FENCE_RE = re.compile(r"^```markdown[ \t]*\n(.*?)^```[ \t]*$", re.S | re.M)
_SCHEMA_TOOL_RE = re.compile(r"\bcodetrail_[a-z0-9_]+\b", re.IGNORECASE)
_DENIED_BARE_TOOL_RE = re.compile(
    r"\b(?:bash|read|grep|glob|edit|task)\b",
    re.IGNORECASE,
)


class BuildPromptError(RuntimeError):
    """The shipped prompt document cannot safely be installed."""


def extract_build_prompt(text: str) -> str:
    """Extract and validate the document's single ``markdown`` fenced block.

    The deny-name check deliberately masks ``codetrail_*`` schema names first:
    a future prompt may mention ``codetrail_read_file`` without accidentally
    being classified as teaching OpenCode's bare ``read`` tool.
    """
    blocks = _PROMPT_FENCE_RE.findall(text)
    if len(blocks) != 1:
        raise BuildPromptError(
            "opencode-build-prompt.md must contain exactly one markdown fenced "
            f"block; found {len(blocks)}"
        )
    body = blocks[0]
    if not body.strip():
        raise BuildPromptError("the canonical build prompt is empty")
    if len(body) > BUILD_PROMPT_MAX_CHARS:
        raise BuildPromptError(
            f"canonical build prompt is {len(body)} chars; maximum is "
            f"{BUILD_PROMPT_MAX_CHARS}"
        )
    schema_masked = _SCHEMA_TOOL_RE.sub("", body)
    denied = sorted(
        {match.group(0).lower() for match in _DENIED_BARE_TOOL_RE.finditer(schema_masked)}
    )
    if denied:
        raise BuildPromptError(
            "canonical build prompt teaches denied bare OpenCode tools: "
            + ", ".join(denied)
        )
    return body


def build_prompt_path(home: Path) -> Path:
    """Return the canonical per-user prompt artifact path."""
    return home / BUILD_PROMPT_RELATIVE_PATH


def build_prompt_reference(path: Path) -> str:
    """Return OpenCode's file-reference syntax for an absolute prompt path."""
    if not path.is_absolute():
        raise ValueError(f"build prompt path must be absolute: {path}")
    raw = str(path)
    if any(char in raw for char in ("\n", "\r", "{", "}")):
        raise ValueError("build prompt path contains a character unsafe for {file:...}")
    return f"{{file:{raw}}}"


def _file_reference_path(reference: str) -> Path | None:
    if not reference.startswith("{file:") or not reference.endswith("}"):
        return None
    raw = reference[len("{file:"):-1]
    if not raw or any(char in raw for char in ("\n", "\r", "{", "}")):
        return None
    path = Path(raw)
    return path if path.is_absolute() else None


def _is_managed_reference(reference: str) -> bool:
    path = _file_reference_path(reference)
    if path is None:
        return False
    suffix = BUILD_PROMPT_RELATIVE_PATH.parts
    return len(path.parts) >= len(suffix) and path.parts[-len(suffix):] == suffix


def apply_build_prompt_contract(
    data: dict[str, Any], reference: str, *, install_if_missing: bool = True
) -> tuple[list[str], list[str], list[str]]:
    """Merge ``agent.build.prompt`` in place without replacing custom values.

    Missing objects/keys are installed only when ``install_if_missing`` is true.
    A reference to CodeTrail's canonical managed relative path may be moved to
    the current canonical absolute path.  Every other existing string is
    user-owned and is only warned about.  Wrong JSON types are returned as
    blocking errors and no mutation is performed.
    """
    changes: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []

    if not isinstance(data, dict):
        return changes, warnings, [
            f"OpenCode config root must be a JSON object, got {type(data).__name__}"
        ]
    if not isinstance(reference, str) or not _is_managed_reference(reference):
        return changes, warnings, [
            "managed build prompt reference must be an absolute "
            "{file:.../.config/codetrail/opencode-build-prompt.md} string"
        ]

    agent_present = "agent" in data
    agent = data.get("agent")
    if agent_present and not isinstance(agent, dict):
        errors.append(
            f"agent must be a JSON object, got {type(agent).__name__}"
        )
        return changes, warnings, errors

    build_present = agent_present and "build" in agent
    build = agent.get("build") if isinstance(agent, dict) else None
    if build_present and not isinstance(build, dict):
        errors.append(
            f"agent.build must be a JSON object, got {type(build).__name__}"
        )
        return changes, warnings, errors

    prompt_present = build_present and "prompt" in build
    prompt = build.get("prompt") if isinstance(build, dict) else None
    if prompt_present and not isinstance(prompt, str):
        errors.append(
            "agent.build.prompt must be a string, got "
            f"{type(prompt).__name__}"
        )
        return changes, warnings, errors

    if not install_if_missing and not prompt_present:
        return changes, warnings, errors

    if not agent_present:
        data["agent"] = {"build": {"prompt": reference}}
        changes.append("installed agent.build.prompt managed file reference")
        return changes, warnings, errors
    assert isinstance(agent, dict)

    if not build_present:
        agent["build"] = {"prompt": reference}
        changes.append("installed agent.build.prompt managed file reference")
        return changes, warnings, errors
    assert isinstance(build, dict)

    if not prompt_present:
        build["prompt"] = reference
        changes.append("installed agent.build.prompt managed file reference")
    elif prompt == reference:
        pass
    elif _is_managed_reference(prompt):
        build["prompt"] = reference
        changes.append(
            f"updated managed agent.build.prompt reference: {prompt!r} -> {reference!r}"
        )
    else:
        warnings.append(
            "agent.build.prompt is user-customised; preserving its existing value "
            "instead of installing the CodeTrail managed reference"
        )

    return changes, warnings, errors
