#!/usr/bin/env python3
"""Strict, stdlib-only deployment profile loader for CodeTrail llama-server roles.

JSON is treated as data only.  The loader never sources or evaluates profile
content, and server commands are built from a closed parameter allowlist.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parent
PROFILE_DIR = REPO_ROOT / "deployment_profiles"
DEFAULT_PROFILE_PATH = PROFILE_DIR / "defaults.json"
ROLES = ("main", "embedding", "reranker", "vl")

_TOP_LEVEL_KEYS = {
    "schema_version",
    "name",
    "extends",
    "description",
    "verification",
    "hardware",
    "services",
}
_LOCAL_TOP_LEVEL_KEYS = {"schema_version", "profile", "services"}
_SERVICE_KEYS = {
    "model",
    "mmproj",
    "port",
    "base_url",
    "bind",
    "gpu_role",
    "ctx",
    "batch",
    "ubatch",
    "parameters",
}
_BIND_VALUES = {"local", "all-interfaces"}
_COMMON_PARAMETERS = {"gpu_layers", "flash_attention", "no_mmap"}
_ROLE_PARAMETERS = {
    "main": _COMMON_PARAMETERS
    | {
        "jinja",
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "presence_penalty",
        "cache_type_k",
        "cache_type_v",
        "n_cpu_moe",
        "threads",
        "fit",
        "fit_target",
        "parallel",
    },
    "embedding": _COMMON_PARAMETERS | {"embedding", "pooling"},
    "reranker": _COMMON_PARAMETERS | {"embedding", "pooling", "reranking"},
    "vl": _COMMON_PARAMETERS,
}
_BARE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,191}$")
_GPU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:,\-]{0,255}$")
_CACHE_TYPES = {"f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"}
_VERIFICATION_VALUES = {"verified", "unverified"}
_EXTERNAL_PROVIDER_PREFIXES = {
    "anthropic",
    "aws",
    "azure",
    "bedrock",
    "cohere",
    "deepseek",
    "google",
    "groq",
    "mistral",
    "mistral.ai",
    "ollama",
    "openai",
    "openrouter",
    "perplexity",
    "together",
    "vertex",
    "xai",
}

# These paths preserve the old launcher layout when models.json has no entry.
# They live here (not in config, launchers, or README), so one resolver owns them.
_LEGACY_MODEL_PATHS = {
    "bge-m3": ("bge-m3", "bge-m3-f16.gguf", "bge-m3*.gguf"),
    "qwen3-reranker-0.6b": (
        "qwen3-reranker-0.6b",
        "qwen3-reranker-0.6b-q8_0.gguf",
        "qwen3-reranker-0.6b*.gguf",
    ),
    "qwen3.5-9b": ("qwen3.5-9b", "Qwen3.5-9B-Q6_K.gguf", "Qwen3.5-9B*.gguf"),
    "qwen3.5-9b-mmproj-f16": ("qwen3.5-9b", "mmproj-F16.gguf", "mmproj*.gguf"),
}


class ProfileError(ValueError):
    """A deployment profile, override, registry, or environment value is invalid."""


@dataclass(frozen=True)
class ServiceProfile:
    role: str
    model: str | None
    port: int
    base_url: str
    gpu_role: str
    gpu: str
    ctx: int | None
    batch: int | None
    ubatch: int | None
    parameters: dict[str, Any]
    mmproj: str | None = None
    # 安全預設:loopback base_url 只綁 127.0.0.1;要對其他機器開放必須
    # 明確設 "all-interfaces"(舊版一律轉 0.0.0.0,對剛接觸專案者是暗坑)。
    bind: str = "local"


@dataclass(frozen=True)
class DeploymentProfile:
    name: str
    description: str
    verification: str
    hardware: str
    services: dict[str, ServiceProfile]
    selected_profile: str
    local_override: Path | None

    def service(self, role: str) -> ServiceProfile:
        try:
            return self.services[role.lower()]
        except KeyError as exc:
            raise ProfileError(f"unknown service role: {role!r}") from exc


def _reject_control(value: str, where: str) -> str:
    if not value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ProfileError(f"{where} must be a non-empty string without control characters")
    return value


def _expanduser(value: str, where: str) -> Path:
    try:
        return Path(value).expanduser()
    except RuntimeError as exc:
        raise ProfileError(f"{where} contains an unresolvable home-directory reference") from exc


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProfileError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ProfileError(f"non-finite JSON number {value!r} is not allowed")


def _decode_json(raw: str, where: str) -> Any:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ProfileError(f"invalid JSON in {where}: {exc}") from exc
    except ProfileError as exc:
        raise ProfileError(f"invalid JSON in {where}: {exc}") from exc


def _read_json_object(path: Path, where: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileError(f"cannot read {where} {path}: {exc}") from exc
    data = _decode_json(raw, f"{where} {path}")
    if not isinstance(data, dict):
        raise ProfileError(f"{where} root must be a JSON object: {path}")
    return data


def _unknown_keys(data: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ProfileError(f"{where} contains unsupported key(s): {', '.join(unknown)}")


def _validate_model_reference(value: Any, where: str, *, nullable: bool) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ProfileError(f"{where} must be a registry key or absolute GGUF path")
    value = _reject_control(value.strip(), where)
    expanded = _expanduser(value, where)
    if expanded.is_absolute():
        if expanded.suffix.lower() != ".gguf":
            raise ProfileError(f"{where} absolute path must name a .gguf file")
        return value
    if not _BARE_MODEL_RE.fullmatch(value):
        raise ProfileError(f"{where} is not a safe registry key or absolute GGUF path: {value!r}")
    return value


def _validate_nullable_int(value: Any, where: str, *, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ProfileError(f"{where} must be null or an integer in 1..{maximum}")
    return value


def _url_port(value: str, where: str) -> int:
    value = _reject_control(value.strip(), where)
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ProfileError(f"{where} contains an invalid URL") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ProfileError(f"{where} must be an http(s) base URL with a host")
    if parts.username or parts.password or parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ProfileError(f"{where} must not contain credentials, a path, query, or fragment")
    try:
        return parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise ProfileError(f"{where} contains an invalid port") from exc


def _url_with_port(value: str, port: int) -> str:
    parts = urlsplit(value)
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return urlunsplit((parts.scheme, f"{host}:{port}", "", "", ""))


def _validate_parameter(role: str, key: str, value: Any, where: str) -> None:
    if key not in _ROLE_PARAMETERS[role]:
        raise ProfileError(f"{where} parameter {key!r} is not allowed for role {role}")
    if key in {"jinja", "embedding", "reranking", "no_mmap"}:
        if not isinstance(value, bool):
            raise ProfileError(f"{where}.{key} must be boolean")
        if role == "embedding" and key == "embedding" and value is not True:
            raise ProfileError(f"{where}.embedding must remain true")
        if role == "reranker" and key in {"embedding", "reranking"} and value is not True:
            raise ProfileError(f"{where}.{key} must remain true")
        return
    if key in {"gpu_layers", "n_cpu_moe", "threads", "top_k"}:
        # llama.cpp 新版支援 `-ngl auto`(配合 --fit 自動決定 offload 層數)。
        if key == "gpu_layers" and value == "auto":
            return
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProfileError(f"{where}.{key} must be an integer")
        lower = -1 if key == "gpu_layers" else 0
        upper = 4096 if key == "gpu_layers" else 1024
        if not lower <= value <= upper or (key == "threads" and value == 0):
            raise ProfileError(f"{where}.{key} is outside the allowed range")
        return
    if key == "fit":
        if value not in {"on", "off"}:
            raise ProfileError(f"{where}.fit must be on or off")
        return
    if key in {"fit_target", "parallel"}:
        upper = 1_048_576 if key == "fit_target" else 64
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
            raise ProfileError(f"{where}.{key} must be an integer in 1..{upper}")
        return
    if key in {"temperature", "top_p", "min_p", "presence_penalty"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProfileError(f"{where}.{key} must be numeric")
        number = float(value)
        bounds = {
            "temperature": (0.0, 5.0),
            "top_p": (0.0, 1.0),
            "min_p": (0.0, 1.0),
            "presence_penalty": (-2.0, 2.0),
        }[key]
        if not bounds[0] <= number <= bounds[1]:
            raise ProfileError(f"{where}.{key} is outside {bounds[0]}..{bounds[1]}")
        return
    if key == "flash_attention":
        if value not in {"on", "off", "auto"}:
            raise ProfileError(f"{where}.flash_attention must be on, off, or auto")
        return
    if key in {"cache_type_k", "cache_type_v"}:
        if value not in _CACHE_TYPES:
            raise ProfileError(f"{where}.{key} is not an allowed cache type")
        return
    if key == "pooling":
        expected = "rank" if role == "reranker" else "cls"
        if value != expected:
            raise ProfileError(f"{where}.pooling must be {expected!r} for role {role}")
        return
    raise ProfileError(f"{where} parameter {key!r} has no validator")


def _validate_document(data: dict[str, Any], where: str, *, local: bool = False) -> None:
    _unknown_keys(data, _LOCAL_TOP_LEVEL_KEYS if local else _TOP_LEVEL_KEYS, where)
    if data.get("schema_version") != 1:
        raise ProfileError(f"{where}.schema_version must equal 1")
    if local and "profile" in data:
        profile = data["profile"]
        if not isinstance(profile, str):
            raise ProfileError(f"{where}.profile must be a profile name or absolute JSON path")
        _reject_control(profile.strip(), f"{where}.profile")
    if not local:
        for key in ("name", "description", "verification", "hardware"):
            if key in data and not isinstance(data[key], str):
                raise ProfileError(f"{where}.{key} must be a string")
            if key in data:
                _reject_control(data[key].strip(), f"{where}.{key}")
        if "verification" in data and data["verification"] not in _VERIFICATION_VALUES:
            raise ProfileError(f"{where}.verification must be verified or unverified")
        if "extends" in data and not isinstance(data["extends"], str):
            raise ProfileError(f"{where}.extends must be a profile name")

    services = data.get("services", {} if local else None)
    if not isinstance(services, dict):
        raise ProfileError(f"{where}.services must be an object")
    unknown_roles = sorted(set(services) - set(ROLES))
    if unknown_roles:
        raise ProfileError(f"{where}.services contains unknown role(s): {', '.join(unknown_roles)}")
    for role, raw in services.items():
        service_where = f"{where}.services.{role}"
        if not isinstance(raw, dict):
            raise ProfileError(f"{service_where} must be an object")
        _unknown_keys(raw, _SERVICE_KEYS, service_where)
        if "model" in raw:
            _validate_model_reference(raw["model"], f"{service_where}.model", nullable=role == "main")
        if "mmproj" in raw:
            if role != "vl":
                raise ProfileError(f"{service_where}.mmproj is only allowed for vl")
            _validate_model_reference(raw["mmproj"], f"{service_where}.mmproj", nullable=False)
        if "port" in raw:
            _validate_nullable_int(raw["port"], f"{service_where}.port", maximum=65535)
        if "base_url" in raw:
            if not isinstance(raw["base_url"], str):
                raise ProfileError(f"{service_where}.base_url must be a string")
            _url_port(raw["base_url"], f"{service_where}.base_url")
        if "gpu_role" in raw and raw["gpu_role"] not in {"main", "aux"}:
            raise ProfileError(f"{service_where}.gpu_role must be main or aux")
        if "bind" in raw and raw["bind"] not in _BIND_VALUES:
            raise ProfileError(f"{service_where}.bind must be local or all-interfaces")
        for field, maximum in (("ctx", 1_048_576), ("batch", 1_048_576), ("ubatch", 1_048_576)):
            if field in raw:
                _validate_nullable_int(raw[field], f"{service_where}.{field}", maximum=maximum)
        if "parameters" in raw:
            params = raw["parameters"]
            if not isinstance(params, dict):
                raise ProfileError(f"{service_where}.parameters must be an object")
            for key, value in params.items():
                _validate_parameter(role, key, value, f"{service_where}.parameters")


def _merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _profile_path(reference: str) -> Path:
    ref = _reject_control(reference.strip(), "profile reference")
    candidate = _expanduser(ref, "profile reference")
    if candidate.is_absolute():
        if candidate.suffix.lower() != ".json":
            raise ProfileError("absolute deployment profile path must end in .json")
        return candidate
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", ref):
        raise ProfileError("AICODE_PROFILE must be a built-in name or absolute JSON path")
    path = PROFILE_DIR / (ref if ref.endswith(".json") else f"{ref}.json")
    if not path.is_file():
        available = ", ".join(sorted(p.stem for p in PROFILE_DIR.glob("*.json") if p.stem != "defaults"))
        raise ProfileError(f"unknown deployment profile {ref!r}; available: {available}")
    return path


def _load_profile_chain(reference: str, seen: set[Path] | None = None) -> tuple[dict[str, Any], Path]:
    raw_path = _profile_path(reference)
    try:
        path = raw_path.resolve()
    except (OSError, RuntimeError) as exc:
        raise ProfileError(f"deployment profile path cannot be resolved: {raw_path}") from exc
    visited = set() if seen is None else seen
    if path in visited:
        raise ProfileError(f"deployment profile extends cycle at {path}")
    visited.add(path)
    data = _read_json_object(path, "deployment profile")
    _validate_document(data, f"deployment profile {path}")
    parent = data.get("extends")
    if parent:
        parent_data, _ = _load_profile_chain(parent, visited)
        return _merge(parent_data, {k: v for k, v in data.items() if k != "extends"}), path
    return data, path


def local_override_path(environ: Mapping[str, str] | None = None) -> Path | None:
    env = environ if environ is not None else os.environ
    explicit = (env.get("AICODE_DEPLOYMENT_CONFIG") or "").strip()
    if explicit:
        path = _expanduser(explicit, "AICODE_DEPLOYMENT_CONFIG")
        if not path.is_absolute():
            raise ProfileError("AICODE_DEPLOYMENT_CONFIG must be an absolute path")
        if path.suffix.lower() != ".json":
            raise ProfileError("AICODE_DEPLOYMENT_CONFIG must point to a .json file")
        return path
    home = (env.get("HOME") or env.get("USERPROFILE") or "").strip()
    return Path(home) / ".config" / "codetrail" / "deployment.json" if home else None


def _first_env(env: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def _parse_env_int(value: str, name: str) -> int:
    if not value.isdecimal():
        raise ProfileError(f"{name} must be a positive integer")
    return int(value)


def _environment_model(value: str, role: str, where: str) -> str:
    """Keep legacy custom-provider/main-model syntax compatible.

    Artifact profiles themselves only accept registry keys or absolute paths.
    AICODE_MODEL historically also accepts ``custom-provider/key``; strip that
    provider here exactly as the main-model resolver does, while refusing known
    external providers.
    """
    path = _expanduser(value, where)
    if role != "main" or path.is_absolute() or "/" not in value:
        return value
    provider, bare = value.split("/", 1)
    if provider.lower() in _EXTERNAL_PROVIDER_PREFIXES:
        raise ProfileError(f"{where} uses external provider prefix {provider!r}")
    if not _BARE_MODEL_RE.fullmatch(provider) or not _BARE_MODEL_RE.fullmatch(bare):
        raise ProfileError(f"{where} is not a safe custom-provider/model identifier")
    return bare


# role → 欄位 → 可覆寫該欄位的 env 名稱。這是 runtime override 的唯一定義處;
# RUNTIME_OVERRIDE_ENV_KEYS 由此推導,set_config / start.sh 產生器 / 測試一律引用它,
# 不再各自維護清單(GPT 評估 #13:清單分叉會讓 .bashrc 舊變數蓋過新設定)。
_ENV_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "main": {
            "model": ("AICODE_MODEL",),
            "base_url": ("AICODE_LLAMA_BASE_URL",),
            "port": ("MAIN_PORT", "AICODE_MAIN_PORT"),
            "bind": ("MAIN_BIND", "AICODE_BIND"),
            "ctx": ("MAIN_CTX", "AICODE_MAIN_CTX"),
            "batch": ("MAIN_BATCH", "AICODE_MAIN_BATCH"),
            "ubatch": ("MAIN_UBATCH", "AICODE_MAIN_UBATCH"),
        },
        "embedding": {
            "model": ("EMBED_MODEL", "AICODE_EMBED_MODEL"),
            "base_url": ("AICODE_LLAMA_EMBED_BASE_URL",),
            "port": ("EMBED_PORT", "AICODE_EMBED_PORT"),
            "bind": ("EMBED_BIND", "AICODE_BIND"),
            "ctx": ("EMBED_CTX", "AICODE_EMBED_CTX"),
            "batch": ("EMBED_BATCH", "AICODE_EMBED_BATCH"),
            "ubatch": ("EMBED_UBATCH", "AICODE_EMBED_UBATCH"),
        },
        "reranker": {
            "model": ("RERANK_MODEL", "AICODE_RERANK_MODEL"),
            "base_url": ("AICODE_LLAMA_RERANK_BASE_URL",),
            "port": ("RERANK_PORT", "AICODE_RERANK_PORT"),
            "bind": ("RERANK_BIND", "AICODE_BIND"),
            "ctx": ("RERANK_CTX", "AICODE_RERANK_CTX"),
            "batch": ("RERANK_BATCH", "AICODE_RERANK_BATCH"),
            "ubatch": ("RERANK_UBATCH", "AICODE_RERANK_UBATCH"),
        },
        "vl": {
            "model": ("VL_GGUF", "AICODE_VL_MODEL"),
            "mmproj": ("VL_MMPROJ", "AICODE_VL_MMPROJ"),
            "base_url": ("AICODE_LLAMA_VL_BASE_URL",),
            "port": ("VL_PORT", "AICODE_VL_PORT"),
            "bind": ("VL_BIND", "AICODE_BIND"),
            "ctx": ("VL_CTX", "AICODE_VL_CTX"),
            "batch": ("VL_BATCH", "AICODE_VL_BATCH"),
            "ubatch": ("VL_UBATCH", "AICODE_VL_UBATCH"),
        },
}

# 會影響有效 deployment 設定的全部環境變數(供 set_config / start.sh / 測試清理用):
# _ENV_FIELDS 推導的 per-role 覆寫 + profile/registry 選擇 + GPU selector。
RUNTIME_OVERRIDE_ENV_KEYS: tuple[str, ...] = tuple(sorted(
    {name for fields in _ENV_FIELDS.values() for names in fields.values() for name in names}
    | {
        "AICODE_PROFILE",
        "AICODE_DEPLOYMENT_CONFIG",
        "AICODE_MODEL_REGISTRY",
        "AICODE_MODEL_REGISTRY_FILE",
        "MAIN_GPU",
        "AUX_GPU",
        "EMBED_GPU",
        "RERANK_GPU",
        "VL_GPU",
        "CUDA_VISIBLE_DEVICES",
    }
))


def _environment_overlay(env: Mapping[str, str], current: dict[str, Any]) -> dict[str, Any]:
    services: dict[str, Any] = {}
    for role, fields in _ENV_FIELDS.items():
        role_overlay: dict[str, Any] = {}
        base_was_set = False
        port_was_set = False
        for field, names in fields.items():
            value = _first_env(env, *names)
            if value is None:
                continue
            if field in {"port", "ctx", "batch", "ubatch"}:
                role_overlay[field] = _parse_env_int(value, names[0])
            elif field == "model":
                role_overlay[field] = _environment_model(value, role, names[0])
            else:
                role_overlay[field] = value
            base_was_set = base_was_set or field == "base_url"
            port_was_set = port_was_set or field == "port"
        if base_was_set and not port_was_set:
            role_overlay["port"] = _url_port(role_overlay["base_url"], f"environment {role} base_url")
        elif port_was_set and not base_was_set:
            old_url = str(current["services"][role]["base_url"])
            role_overlay["base_url"] = _url_with_port(old_url, role_overlay["port"])
        if role_overlay:
            services[role] = role_overlay
    return {"services": services} if services else {}


def _validate_effective(data: dict[str, Any], where: str) -> None:
    _validate_document(data, where)
    missing_top = sorted({"name", "description", "verification", "hardware", "services"} - set(data))
    if missing_top:
        raise ProfileError(f"{where} is missing top-level field(s): {', '.join(missing_top)}")
    services = data["services"]
    if set(services) != set(ROLES):
        missing = sorted(set(ROLES) - set(services))
        raise ProfileError(f"{where} is missing service role(s): {', '.join(missing)}")
    for role in ROLES:
        raw = services[role]
        missing = sorted({"model", "port", "base_url", "gpu_role", "ctx", "batch", "ubatch", "parameters"} - set(raw))
        if missing:
            raise ProfileError(f"{where}.services.{role} is missing field(s): {', '.join(missing)}")
        if role != "main" and raw["model"] is None:
            raise ProfileError(f"{where}.services.{role}.model may not be null")
        if role == "vl" and not raw.get("mmproj"):
            raise ProfileError(f"{where}.services.vl.mmproj is required")
        expected_gpu_role = "main" if role == "main" else "aux"
        if raw["gpu_role"] != expected_gpu_role:
            raise ProfileError(
                f"{where}.services.{role}.gpu_role must remain {expected_gpu_role!r}"
            )
        if _url_port(raw["base_url"], f"{where}.services.{role}.base_url") != raw["port"]:
            raise ProfileError(f"{where}.services.{role} port and base_url port do not match")
        batch = raw["batch"]
        ubatch = raw["ubatch"]
        ctx = raw["ctx"]
        if batch is not None and ctx is not None and batch > ctx:
            raise ProfileError(f"{where}.services.{role}.batch may not exceed ctx")
        if ubatch is not None and batch is not None and ubatch > batch:
            raise ProfileError(f"{where}.services.{role}.ubatch may not exceed batch")


def _gpu_for(role: str, gpu_role: str, env: Mapping[str, str]) -> str:
    per_role = {
        "main": ("MAIN_GPU",),
        "embedding": ("EMBED_GPU",),
        "reranker": ("RERANK_GPU",),
        "vl": ("VL_GPU",),
    }[role]
    shared = ("MAIN_GPU",) if gpu_role == "main" else ("AUX_GPU",)
    value = _first_env(env, *per_role, *shared, "CUDA_VISIBLE_DEVICES") or ""
    if value and not _GPU_RE.fullmatch(value):
        raise ProfileError(f"GPU selector for {role} contains unsupported characters: {value!r}")
    return value


def load_effective_profile(
    environ: Mapping[str, str] | None = None,
    *,
    profile: str | None = None,
    cli_env: Mapping[str, str] | None = None,
) -> DeploymentProfile:
    """Load defaults < selected profile < local override < env/CLI overrides."""
    env = dict(os.environ if environ is None else environ)
    if cli_env:
        env.update({key: str(value) for key, value in cli_env.items()})

    override_path = local_override_path(env)
    local_data: dict[str, Any] | None = None
    if override_path and override_path.is_file():
        local_data = _read_json_object(override_path, "local deployment override")
        _validate_document(local_data, f"local deployment override {override_path}", local=True)

    selected = (profile or env.get("AICODE_PROFILE") or "").strip()
    if not selected and local_data:
        selected = str(local_data.get("profile") or "").strip()
    selected = selected or "defaults"

    data, selected_path = _load_profile_chain(selected)
    if local_data:
        data = _merge(data, {"services": local_data.get("services", {})})
    data = _merge(data, _environment_overlay(env, data))
    _validate_effective(data, "effective deployment profile")

    services: dict[str, ServiceProfile] = {}
    for role in ROLES:
        raw = data["services"][role]
        services[role] = ServiceProfile(
            role=role,
            model=raw["model"],
            mmproj=raw.get("mmproj"),
            port=raw["port"],
            base_url=raw["base_url"].rstrip("/"),
            bind=raw.get("bind") or "local",
            gpu_role=raw["gpu_role"],
            gpu=_gpu_for(role, raw["gpu_role"], env),
            ctx=raw["ctx"],
            batch=raw["batch"],
            ubatch=raw["ubatch"],
            parameters=dict(raw["parameters"]),
        )
    return DeploymentProfile(
        name=str(data["name"]),
        description=str(data["description"]),
        verification=str(data["verification"]),
        hardware=str(data["hardware"]),
        services=services,
        selected_profile=selected_path.stem,
        local_override=override_path if local_data else None,
    )


def load_model_registry(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = environ if environ is not None else os.environ
    raw = (env.get("AICODE_MODEL_REGISTRY") or "").strip()
    source = "AICODE_MODEL_REGISTRY"
    if raw:
        data = _decode_json(raw, source)
    else:
        explicit = (env.get("AICODE_MODEL_REGISTRY_FILE") or "").strip()
        if explicit:
            path = _expanduser(explicit, "AICODE_MODEL_REGISTRY_FILE")
        else:
            home = (env.get("HOME") or env.get("USERPROFILE") or "").strip()
            path = Path(home) / ".config" / "codetrail" / "models.json" if home else Path()
        if not path or not path.is_file():
            return {}
        source = str(path)
        data = _read_json_object(path, "model registry")
    if not isinstance(data, dict):
        raise ProfileError(f"model registry {source} must be a JSON object")
    registry: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not _BARE_MODEL_RE.fullmatch(key):
            raise ProfileError(f"model registry {source} has an invalid key: {key!r}")
        if not isinstance(value, str):
            raise ProfileError(f"model registry entry {key!r} must be a path string")
        value = _reject_control(value.strip(), f"model registry entry {key!r}")
        path = _expanduser(value, f"model registry entry {key!r}")
        if not path.is_absolute() or path.suffix.lower() != ".gguf":
            raise ProfileError(f"model registry entry {key!r} must resolve to an absolute .gguf path")
        registry[key] = str(path)
    return registry


def resolve_model_reference(
    reference: str | None,
    environ: Mapping[str, str] | None = None,
    *,
    must_exist: bool = False,
) -> str:
    if reference is None:
        raise ProfileError("main model is unset; set AICODE_MODEL or a registry-backed profile model")
    env = environ if environ is not None else os.environ
    ref = _validate_model_reference(reference, "model reference", nullable=False)
    assert ref is not None
    expanded = _expanduser(ref, "model reference")
    if expanded.is_absolute():
        path = expanded
    else:
        registry = load_model_registry(env)
        registered = registry.get(ref)
        if registered:
            path = Path(registered)
        elif ref in _LEGACY_MODEL_PATHS:
            directory, filename, pattern = _LEGACY_MODEL_PATHS[ref]
            models_dir = _expanduser(env.get("MODELS_DIR") or "~/models", "MODELS_DIR")
            base = models_dir / directory
            preferred = base / filename
            matches = sorted(base.glob(pattern)) if base.is_dir() else []
            path = preferred if preferred.is_file() or not matches else matches[0]
        else:
            raise ProfileError(
                f"model registry key {ref!r} is not defined; add it to "
                "~/.config/codetrail/models.json or use an absolute GGUF path"
            )
    try:
        path = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ProfileError(f"model artifact path cannot be resolved: {path}") from exc
    if must_exist and not path.is_file():
        raise ProfileError(f"model artifact does not exist: {path}")
    return str(path)


def bind_host(service: ServiceProfile) -> str:
    """loopback base_url 預設只綁 127.0.0.1;`bind: "all-interfaces"` 才綁 0.0.0.0。

    llama-server 沒有內建認證,綁 0.0.0.0 等於把模型 API 開放給整個網段;
    這必須是使用者的明確選擇(deployment.json 的 bind 欄位、AICODE_BIND=all-interfaces
    或 set_config 的 --allow-remote),不能是 localhost 的靜默轉譯。
    """
    host = urlsplit(service.base_url).hostname or ""
    if host == "localhost" or host.startswith("127.") or host == "::1":
        return "0.0.0.0" if service.bind == "all-interfaces" else "127.0.0.1"
    return host


def build_server_command(
    service: ServiceProfile,
    llama_bin: str,
    environ: Mapping[str, str] | None = None,
    *,
    must_exist: bool = False,
) -> list[str]:
    """Build argv only from validated structured fields and the parameter allowlist."""
    model_path = resolve_model_reference(service.model, environ, must_exist=must_exist)
    command = [llama_bin, "-m", model_path]
    if service.mmproj:
        command.extend(["--mmproj", resolve_model_reference(service.mmproj, environ, must_exist=must_exist)])
    command.extend(["--host", bind_host(service), "--port", str(service.port)])
    if service.ctx is not None:
        command.extend(["-c", str(service.ctx)])
    if service.batch is not None:
        command.extend(["-b", str(service.batch)])
    if service.ubatch is not None:
        command.extend(["-ub", str(service.ubatch)])

    p = service.parameters
    if p.get("embedding"):
        command.append("--embedding")
    if "pooling" in p:
        command.extend(["--pooling", str(p["pooling"])])
    if p.get("reranking"):
        command.append("--reranking")
    if "gpu_layers" in p:
        command.extend(["-ngl", str(p["gpu_layers"])])
    if p.get("jinja"):
        command.append("--jinja")
    parameter_flags = (
        ("temperature", "--temp"),
        ("top_p", "--top-p"),
        ("top_k", "--top-k"),
        ("min_p", "--min-p"),
        ("presence_penalty", "--presence-penalty"),
        ("cache_type_k", "--cache-type-k"),
        ("cache_type_v", "--cache-type-v"),
        ("n_cpu_moe", "--n-cpu-moe"),
        ("fit", "--fit"),
        ("fit_target", "--fit-target"),
        ("parallel", "-np"),
        ("flash_attention", "-fa"),
        ("threads", "-t"),
    )
    for key, flag in parameter_flags:
        if key in p:
            command.extend([flag, str(p[key]).lower() if isinstance(p[key], bool) else str(p[key])])
    if p.get("no_mmap"):
        command.append("--no-mmap")
    if service.gpu:
        command = ["env", f"CUDA_VISIBLE_DEVICES={service.gpu}", *command]
    return command


def profile_as_dict(profile: DeploymentProfile, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    services: dict[str, Any] = {}
    for role, service in profile.services.items():
        item = {
            "model": service.model,
            "port": service.port,
            "base_url": service.base_url,
            "bind": service.bind,
            "gpu_role": service.gpu_role,
            "gpu": service.gpu,
            "ctx": service.ctx,
            "batch": service.batch,
            "ubatch": service.ubatch,
            "parameters": service.parameters,
        }
        if service.mmproj:
            item["mmproj"] = service.mmproj
        try:
            item["model_path"] = resolve_model_reference(service.model, environ)
        except ProfileError:
            item["model_path"] = None
        if service.mmproj:
            try:
                item["mmproj_path"] = resolve_model_reference(service.mmproj, environ)
            except ProfileError:
                item["mmproj_path"] = None
        services[role] = item
    return {
        "schema_version": 1,
        "name": profile.name,
        "selected_profile": profile.selected_profile,
        "description": profile.description,
        "verification": profile.verification,
        "hardware": profile.hardware,
        "local_override": str(profile.local_override) if profile.local_override else None,
        "services": services,
    }


def runtime_environment(profile: DeploymentProfile, *, include_main_model: bool = False) -> dict[str, str]:
    services = profile.services
    values = {
        "AICODE_LLAMA_BASE_URL": services["main"].base_url,
        "AICODE_LLAMA_EMBED_BASE_URL": services["embedding"].base_url,
        "AICODE_LLAMA_RERANK_BASE_URL": services["reranker"].base_url,
        "AICODE_LLAMA_VL_BASE_URL": services["vl"].base_url,
        "AICODE_EMBED_MODEL": services["embedding"].model or "",
        "AICODE_RERANK_MODEL": services["reranker"].model or "",
        "AICODE_VL_MODEL": services["vl"].model or "",
        "AICODE_VL_MMPROJ": services["vl"].mmproj or "",
        "AICODE_EFFECTIVE_PROFILE": profile.selected_profile,
    }
    if include_main_model and services["main"].model:
        values["AICODE_MODEL"] = services["main"].model or ""
    return values


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve and validate CodeTrail deployment profiles")
    parser.add_argument("--profile", help="built-in profile name or absolute JSON profile path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="print the effective profile as JSON")
    validate = sub.add_parser("validate", help="validate profile and optionally require model files")
    validate.add_argument("--require-files", action="store_true")
    env_parser = sub.add_parser("env", help="emit runtime environment assignments")
    env_parser.add_argument("--null", action="store_true", help="NUL-delimit assignments for safe shell import")
    env_parser.add_argument("--include-main-model", action="store_true")
    get_parser = sub.add_parser("get", help="print one effective service field")
    get_parser.add_argument("role", choices=ROLES)
    get_parser.add_argument("field", choices=("model", "mmproj", "port", "base_url", "bind", "gpu_role", "gpu", "ctx", "batch", "ubatch"))
    exec_parser = sub.add_parser("exec", help="exec one role directly (for systemd or another supervisor)")
    exec_parser.add_argument("role", choices=ROLES)
    exec_parser.add_argument("--llama-bin", default=os.environ.get("LLAMA_BIN") or str(Path.home() / "llama.cpp" / "build" / "bin" / "llama-server"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        profile = load_effective_profile(profile=args.profile)
        if args.command == "show":
            print(json.dumps(profile_as_dict(profile), ensure_ascii=False, indent=2, sort_keys=True))
        elif args.command == "validate":
            if args.require_files:
                for service in profile.services.values():
                    resolve_model_reference(service.model, must_exist=True)
                    if service.mmproj:
                        resolve_model_reference(service.mmproj, must_exist=True)
            print(f"profile={profile.selected_profile} verification={profile.verification} valid")
        elif args.command == "env":
            values = runtime_environment(profile, include_main_model=args.include_main_model)
            separator = "\0" if args.null else "\n"
            sys.stdout.write(separator.join(f"{key}={value}" for key, value in values.items()))
            if args.null:
                sys.stdout.write("\0")
            else:
                sys.stdout.write("\n")
        elif args.command == "get":
            value = getattr(profile.service(args.role), args.field)
            print("" if value is None else value)
        elif args.command == "exec":
            command = build_server_command(
                profile.service(args.role),
                args.llama_bin,
                os.environ,
                must_exist=True,
            )
            os.execvpe(command[0], command, dict(os.environ))
        return 0
    except (OSError, ProfileError) as exc:
        print(f"[deployment-profile] ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
