"""Local DSpark dependency checks without downloads or a fallback backend.

The configured draft is operator-selected and target-specific. File and build
checks do not claim target compatibility; llama-server validates that on load,
then the launcher checks live speculative activation through /slots.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import process_env

if TYPE_CHECKING:
    from deployment_profile import ServiceProfile

_SHARDS = re.compile(r"(.+)-([0-9]{5})-of-([0-9]{5})(\.gguf)", re.IGNORECASE)
_HELP_FLAGS = ("--spec-type", "--spec-draft-model", "--spec-draft-n-max")
_HELP_TIMEOUT_SECONDS = 10


def resolve_dspark_draft(
    service: ServiceProfile, *, must_exist: bool = False,
    registry_file: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve one enabled draft; file validation also checks all GGUF shards."""
    from deployment_profile import ProfileError, resolve_model_reference

    if service.role != "main" or service.deployment_mode == "client":
        raise ProfileError("DSpark requires a local main service on the model host")
    if service.dspark is None:
        raise ProfileError("DSpark is disabled; there is no configured draft")
    path = Path(resolve_model_reference(
        service.dspark.draft_model, environ, must_exist=must_exist,
        registry_file=registry_file,
    ))
    if must_exist:
        match = _SHARDS.fullmatch(path.name)
        if match:
            count = int(match[3])
            if int(match[2]) != 1 or not 1 <= count <= 99999:
                raise ProfileError("DSpark draft must select the first GGUF shard and a complete shard count")
            for index in range(1, count + 1):
                shard = path.with_name(f"{match[1]}-{index:05d}-of-{count:05d}{match[4]}")
                if not shard.is_file():
                    raise ProfileError(f"DSpark draft shard does not exist: {shard}")
    return str(path)


def validate_dspark_runtime(
    service: ServiceProfile, llama_bin: str, *,
    registry_file: str | Path | None = None,
) -> None:
    """Fail before launch if enabled DSpark cannot use its local dependencies.

    Off is a strict no-op, including no draft resolution, subprocess or GPU probe.
    Help-specific exit codes vary by build; capability comes from emitted exact
    option/type tokens, never a substring of a different flag or speculative type.
    """
    if service.dspark is None:
        return
    from deployment_profile import ProfileError

    resolve_dspark_draft(service, must_exist=True, registry_file=registry_file)
    binary = Path(llama_bin).expanduser()
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise ProfileError(f"DSpark llama-server does not exist or is not executable: {binary}")
    try:
        result = process_env.run(
            [str(binary), "--help"], server_env=True, capture_output=True,
            text=True, timeout=_HELP_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, process_env.SubprocessError, UnicodeError) as exc:
        raise ProfileError(f"cannot verify DSpark support in {binary}: {exc}") from exc
    help_text = f"{result.stdout or ''}\n{result.stderr or ''}"
    missing = [
        token for token in (*_HELP_FLAGS, "draft-dspark")
        if re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])", help_text) is None
    ]
    if missing:
        raise ProfileError(
            f"DSpark requires llama-server support for {', '.join(missing)}; "
            f"not advertised by {binary} --help (exit {result.returncode}). "
            "Use a DSpark-capable build or turn DSpark off in set_config."
        )
