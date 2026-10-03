"""Prompt-free model identities for deployment checks and resumable jobs.

Remote identities verify an operator-versioned alias against live /props. They
are deliberately not presented as a locally verified hash of remote weights.
No missing identity is replaced by a configured name or a previous result.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

import deployment_profile


class ModelIdentityError(RuntimeError):
    pass


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _file_digest(path: str | Path) -> str:
    """Hash an opened regular artifact and reject a concurrently changed file."""
    try:
        with open(path, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ModelIdentityError("model artifact is not a regular file")
            digest = hashlib.sha256()
            while chunk := stream.read(4 * 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        current = os.stat(path)
        fields = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if fields(before) != fields(after) or fields(after) != fields(current):
            raise ModelIdentityError("model artifact changed while computing identity")
        return digest.hexdigest()
    except OSError as exc:
        raise ModelIdentityError(f"cannot verify model artifact: {exc}") from exc


def artifact_digest(path: str | Path) -> str:
    """Include every GGUF shard, not just the first file passed to llama-server."""
    path = Path(path)
    match = re.fullmatch(r"(.+)-([0-9]{5})-of-([0-9]{5})\.gguf", path.name, re.IGNORECASE)
    if not match:
        return _file_digest(path)
    count = int(match[3])
    if int(match[2]) != 1 or not 1 <= count <= 99999:
        raise ModelIdentityError("sharded model must identify its first shard and complete shard count")
    return _digest([_file_digest(path.with_name(f"{match[1]}-{i:05d}-of-{count:05d}.gguf"))
                    for i in range(1, count + 1)])


def _configured_dspark_identity(dspark: deployment_profile.DSparkConfig,
                                *, registry_file: str | Path | None = None) -> dict:
    """Configured draft identity, not evidence of live speculative activation."""
    try:
        draft = deployment_profile.resolve_model_reference(
            dspark.draft_model, registry_file=registry_file, must_exist=True)
    except deployment_profile.ProfileError as exc:
        raise ModelIdentityError(f"cannot verify DSpark draft artifact: {exc}") from exc
    return {"spec_type": "draft-dspark", "draft_artifact_sha256": artifact_digest(draft),
            "draft_n_max": dspark.draft_n_max}


def versioned_alias(role: str, path: str | Path, mmproj: str | Path | None = None,
                    *, dspark: deployment_profile.DSparkConfig | None = None,
                    registry_file: str | Path | None = None) -> str:
    digest = artifact_digest(path)
    if mmproj:
        digest = _digest([digest, artifact_digest(mmproj)])
    if dspark is not None:
        if role != "main":
            raise ModelIdentityError("DSpark identity is supported only for the main service")
        digest = _digest([digest, _configured_dspark_identity(dspark, registry_file=registry_file)])
    return f"ct-{role}-{digest}"


def _capture_model_identity(role: str, *, profile=None, props=None) -> dict:
    """Return a JSON identity/fingerprint or raise; only /props GET is used.

    ``props`` is an already observed live /props object (diagnostic callers).
    It is never persisted or accepted from deployment/client configuration.
    A configured DSpark draft contributes its local artifact identity only;
    /props does not establish whether speculative decoding is active.
    """
    profile = profile or deployment_profile.load_effective_profile()
    service = profile.service(role)
    import endpoint_policy
    endpoint_policy.ensure_allowed(service.base_url + "/props", role, split=profile.mode == "client")
    if props is None:
        import llama_client
        props = llama_client.get_props(service.base_url)
    if not isinstance(props, dict):
        raise ModelIdentityError(
            f"{role}: live /props unavailable at {service.base_url}/props; identity not verified. "
            f"Check the {role} llama-server (deployment.json 的 services.{role}) "
            "endpoint and server status."
        )
    loaded = props.get("model_path")
    alias = props.get("model_alias")
    if not isinstance(loaded, str) or not loaded or loaded == "none":
        raise ModelIdentityError(f"{role}: live model_path missing; identity not verified")
    artifact = None
    projector = None
    configured_dspark = None
    if profile.mode == "client":
        expected = service.identity_alias
        if not expected or alias != expected or service.model != expected:
            raise ModelIdentityError(f"{role}: live model_alias does not match the versioned identity_alias")
        kind = "declared-runtime-alias"
    else:
        expected_path = deployment_profile.resolve_model_reference(
            service.model, registry_file=profile.registry_file, must_exist=True)
        try:
            matches = os.path.samefile(expected_path, loaded)
        except OSError:
            matches = False
        if not matches:
            raise ModelIdentityError(f"{role}: live model_path differs from the selected artifact")
        artifact = artifact_digest(expected_path)
        if service.mmproj:
            projector = artifact_digest(deployment_profile.resolve_model_reference(
                service.mmproj, registry_file=profile.registry_file, must_exist=True))
        if service.dspark is not None:
            configured_dspark = _configured_dspark_identity(
                service.dspark, registry_file=profile.registry_file)
        if service.identity_alias and alias != service.identity_alias:
            raise ModelIdentityError(f"{role}: live model_alias differs from deployment identity_alias")
        kind = "local-artifact-sha256"
    settings = props.get("default_generation_settings")
    settings = settings if isinstance(settings, dict) else {}
    live = {
        "model_alias": alias,
        "model_path_digest": _digest(loaded),
        "n_ctx": props.get("n_ctx", settings.get("n_ctx")),
        "chat_template_digest": _digest(props.get("chat_template")),
        "chat_template_caps": props.get("chat_template_caps"),
        "build_info": props.get("build_info"),
        "modalities": props.get("modalities"),
    }
    identity = {"schema": 1, "role": role, "model_id": service.model,
                "identity_kind": kind, "artifact_sha256": artifact,
                "projector_sha256": projector, "identity_alias": service.identity_alias,
                "endpoint": service.base_url, "live": live}
    if configured_dspark is not None:
        identity["configured_dspark"] = configured_dspark
    identity["fingerprint"] = _digest(identity)
    return identity


def capture_model_identity(role: str, *, profile=None, props=None) -> dict:
    """Return a verified JSON identity; unavailable/untrusted inputs fail closed."""
    try:
        return _capture_model_identity(role, profile=profile, props=props)
    except ModelIdentityError:
        raise
    except (RuntimeError, ValueError, TypeError, OSError) as exc:
        raise ModelIdentityError(f"{role}: cannot verify live model identity: {exc}") from exc


def capture_model_identities(roles, *, profile=None) -> dict[str, dict]:
    profile = profile or deployment_profile.load_effective_profile()
    return {role: capture_model_identity(role, profile=profile) for role in roles}
