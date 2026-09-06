#!/usr/bin/env python3
"""Strict deployment profile loader for CodeTrail llama-server roles.

No third-party dependency: stdlib plus `process_env`(itself stdlib-only), which
owns the environment every child process — including the exec'd llama-server — gets.
JSON is treated as data only.  The loader never sources or evaluates profile
content, and server commands are built from a closed parameter allowlist.

設定的來源只有兩個:`~/.config/codetrail/deployment.json`(與它選的 profile 檔)
與 argv。這個模組**不從環境變數取任何設定** —— `environ` 參數只拿來定位檔案
(`HOME` / `USERPROFILE`)。同一台機器上可能有兩份安裝,另一份 `~/start.sh` 設的
同名變數會靜默蓋過設定檔,而症狀是「使用者以為在跑 A、實際在跑 B」。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import process_env

ROLES = ("main", "embedding", "reranker", "vl")

#: llama-server 執行檔的最後手段預設(檔案沒寫、argv 沒給時)。
DEFAULT_LLAMA_BIN = "~/llama.cpp/build/bin/llama-server"

#: tmux session 名是 repo 常數,不是設定:launcher / stop / status / set_config
#: 必須指同一組,否則「啟動的那一份」與「停掉的那一份」會是兩個 session。
TMUX_SESSIONS = {"main": "codetrail-main", "aux": "codetrail-rag"}

_TOP_LEVEL_KEYS = {
    "schema_version",
    "name",
    "extends",
    "description",
    "verification",
    "hardware",
    "services",
}
_LOCAL_TOP_LEVEL_KEYS = {"schema_version", "profile", "services", "llama_bin"}
_SERVICE_KEYS = {
    "model",
    "mmproj",
    "port",
    "base_url",
    "bind",
    "gpu_role",
    "gpu",
    "ctx",
    "batch",
    "ubatch",
    "parameters",
}
_BIND_VALUES = {"local", "all-interfaces"}
_COMMON_PARAMETERS = {"gpu_layers", "flash_attention", "no_mmap", "parallel"}
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
        "cpu_moe",
        "n_cpu_moe",
        "threads",
        "fit",
        "fit_target",
    },
    "embedding": _COMMON_PARAMETERS | {"embedding", "pooling", "cache_ram"},
    "reranker": _COMMON_PARAMETERS | {"embedding", "pooling", "reranking", "cache_ram"},
    # VL 最後啟動，可用 llama.cpp --fit 依前兩個 aux 的實際占用保留 VRAM。
    # MoE VL 模型同樣可以把 experts 釘進 RAM。注意 llama.cpp 的 --fit 與 tensor
    # override 互斥（common_params_fit_impl 見到 tensor_buft_overrides 已被設定就
    # abort），所以 set_config 在 VL 套 CPU-MoE 時會改寫 -ngl 99 --fit off。
    "vl": _COMMON_PARAMETERS | {"fit", "fit_target", "cpu_moe", "n_cpu_moe"},
}
_BARE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,191}$")
_GPU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:,\-]{0,255}$")
_CACHE_TYPES = {"f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"}
_VERIFICATION_VALUES = {"verified", "unverified"}

# These paths preserve the old launcher layout when models.json has no entry.
# They live here (not in config, launchers, or README), so one resolver owns them.
_LEGACY_MODEL_PATHS = {
    "bge-m3": ("bge-m3", "bge-m3-f16.gguf", "bge-m3*.gguf"),
    "bge-reranker-v2-m3": (
        "bge-reranker-v2-m3",
        "bge-reranker-v2-m3-Q8_0.gguf",
        "bge-reranker-v2-m3*.gguf",
    ),
    "qwen3-reranker-0.6b": (
        "qwen3-reranker-0.6b",
        "qwen3-reranker-0.6b-q8_0.gguf",
        "qwen3-reranker-0.6b*.gguf",
    ),
    "qwen3.5-9b": ("qwen3.5-9b", "Qwen3.5-9B-Q6_K.gguf", "Qwen3.5-9B*.gguf"),
    "qwen3.5-9b-mmproj-f16": ("qwen3.5-9b", "mmproj-F16.gguf", "mmproj*.gguf"),
}

# 內建 safe-defaults 基底:不宣稱硬體的向下相容預設。所有有效設定都以它墊底,
# set_config 產生的 ~/.config/codetrail/deployment.json 與 env/CLI 覆寫疊在上面。
# 附屬模型預設(bge-m3 / bge-reranker-v2-m3 / qwen3.5-9b)的單一事實來源在這裡;
# check_readme_consistency 會驗證使用者文件與這份預設同步。
_BUILTIN_DEFAULTS: dict[str, Any] = {
    "schema_version": 1,
    "name": "safe-defaults",
    "description": "Backward-compatible CodeTrail llama-server defaults without a hardware claim.",
    "verification": "unverified",
    "hardware": "unspecified",
    "services": {
        "main": {
            "model": None,
            "port": 8080,
            "base_url": "http://localhost:8080",
            "gpu_role": "main",
            "ctx": 65536,
            "batch": None,
            "ubatch": None,
            "parameters": {"gpu_layers": 99, "jinja": True},
        },
        "embedding": {
            "model": "bge-m3",
            "port": 8081,
            "base_url": "http://localhost:8081",
            "gpu_role": "aux",
            "ctx": 8192,
            "batch": 8192,
            "ubatch": 8192,
            "parameters": {
                "gpu_layers": 99,
                "embedding": True,
                "pooling": "cls",
                "cache_ram": 0,
            },
        },
        "reranker": {
            "model": "bge-reranker-v2-m3",
            "port": 8082,
            "base_url": "http://localhost:8082",
            "gpu_role": "aux",
            "ctx": 8192,
            "batch": 8192,
            "ubatch": 8192,
            "parameters": {
                "gpu_layers": 99,
                "embedding": True,
                "pooling": "rank",
                "reranking": True,
                "cache_ram": 0,
            },
        },
        "vl": {
            "model": "qwen3.5-9b",
            "mmproj": "qwen3.5-9b-mmproj-f16",
            "port": 8083,
            "base_url": "http://localhost:8083",
            "gpu_role": "aux",
            "ctx": 8192,
            "batch": None,
            "ubatch": None,
            "parameters": {"gpu_layers": 99},
        },
    },
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
class LauncherOverrides:
    """啟動核心的 argv 覆寫。**只有這幾格**能蓋過 `deployment.json`。

    以前這一層是三十幾個環境變數疊成的 overlay。改成 argv 之後,「誰覆寫了什麼」
    在 `ps` 上看得見,而且另一份安裝的 `~/start.sh` 再也蓋不到。
    `gpus` 的鍵是 role 名加上 `"aux"`(套到三個附屬角色裡沒有自己那格的)。
    """

    main_model: str | None = None
    main_ctx: int | None = None
    main_batch: int | None = None
    main_ubatch: int | None = None
    gpus: Mapping[str, str] = field(default_factory=dict)
    llama_bin: str | None = None


@dataclass(frozen=True)
class DeploymentProfile:
    name: str
    description: str
    verification: str
    hardware: str
    services: dict[str, ServiceProfile]
    selected_profile: str
    local_override: Path | None
    #: llama-server 執行檔的絕對路徑:argv > `deployment.json` 的 `llama_bin` > 預設。
    llama_bin: str = ""
    #: 明確指定的 registry 檔(set_config 驗證用的暫存檔);None = `~/.config/codetrail/models.json`。
    #: 一路交到 `resolve_model_reference` / `build_server_command` / `inspect_deployment`,
    #: 不再有第二條「從環境再查一次」的路。
    registry_file: Path | None = None

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


def _validate_llama_bin(value: Any, where: str) -> str:
    """llama-server 執行檔路徑:非空字串、可展開 `~`、**必須是絕對路徑**。

    存在性不在這裡驗(loader 只驗形狀);但相對路徑一定要擋 —— pane 裡 exec 的
    cwd 不是使用者打指令的地方,`./llama-server` 會變成「看情況指到別的東西」。
    """
    if not isinstance(value, str):
        raise ProfileError(f"{where} must be an absolute path to the llama-server binary")
    path = _expanduser(_reject_control(value.strip(), where), where)
    if not path.is_absolute():
        raise ProfileError(f"{where} must be an absolute path: {value!r}")
    return str(path)


def _validate_gpu_selector(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ProfileError(f"{where} must be a GPU selector string")
    value = value.strip()
    if value and not _GPU_RE.fullmatch(value):
        raise ProfileError(f"{where} contains unsupported characters: {value!r}")
    return value


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


def _validate_parameter(role: str, key: str, value: Any, where: str) -> None:
    if key not in _ROLE_PARAMETERS[role]:
        raise ProfileError(f"{where} parameter {key!r} is not allowed for role {role}")
    if key in {"jinja", "embedding", "reranking", "no_mmap", "cpu_moe"}:
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
    if key == "cache_ram":
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 262_144:
            raise ProfileError(f"{where}.cache_ram must be an integer in 0..262144 MiB")
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
    if local and "llama_bin" in data:
        _validate_llama_bin(data["llama_bin"], f"{where}.llama_bin")
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
        if "gpu" in raw:
            _validate_gpu_selector(raw["gpu"], f"{service_where}.gpu")
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
            if params.get("cpu_moe") and "n_cpu_moe" in params:
                raise ProfileError(
                    f"{service_where}.parameters.cpu_moe and n_cpu_moe are mutually exclusive"
                )
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
    if not candidate.is_absolute():
        raise ProfileError(
            'profile must be "defaults" or an absolute JSON profile path'
        )
    if candidate.suffix.lower() != ".json":
        raise ProfileError("absolute deployment profile path must end in .json")
    return candidate


def _load_profile_chain(reference: str, seen: set[Path] | None = None) -> tuple[dict[str, Any], str]:
    """回傳 (合併後資料, 選用名稱)。名稱是 "defaults" 或絕對路徑檔的 stem。"""
    ref = _reject_control(reference.strip(), "profile reference")
    if ref == "defaults":
        data = json.loads(json.dumps(_BUILTIN_DEFAULTS))
        _validate_document(data, "built-in safe-defaults")
        return data, "defaults"
    raw_path = _profile_path(ref)
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
        return _merge(parent_data, {k: v for k, v in data.items() if k != "extends"}), path.stem
    return data, path.stem


def _home(env: Mapping[str, str]) -> str:
    """`environ` 參數的唯一用途:檔案在哪。只讀 HOME(Windows 的 USERPROFILE)。"""
    return (env.get("HOME") or env.get("USERPROFILE") or "").strip()


def local_override_path(
    environ: Mapping[str, str] | None = None,
    *,
    deployment_config: str | Path | None = None,
) -> Path | None:
    env = environ if environ is not None else os.environ
    if deployment_config:
        path = _expanduser(_reject_control(str(deployment_config).strip(), "--deployment-config"), "--deployment-config")
        if not path.is_absolute():
            raise ProfileError("--deployment-config must be an absolute path")
        if path.suffix.lower() != ".json":
            raise ProfileError("--deployment-config must point to a .json file")
        return path
    home = _home(env)
    return Path(home) / ".config" / "codetrail" / "deployment.json" if home else None


def _overrides_overlay(overrides: LauncherOverrides) -> dict[str, Any]:
    """argv 覆寫 → 與 `deployment.json` 同形狀的 overlay(只有 main 這幾格)。

    值不在這裡另外驗:`_validate_effective` 會用與檔案完全相同的 schema 驗一次,
    所以 `--main-ctx 0` 與檔案裡寫 `"ctx": 0` 得到的是同一個錯誤。
    """
    main: dict[str, Any] = {}
    if overrides.main_model is not None:
        main["model"] = overrides.main_model
    for field_name, value in (
        ("ctx", overrides.main_ctx),
        ("batch", overrides.main_batch),
        ("ubatch", overrides.main_ubatch),
    ):
        if value is not None:
            main[field_name] = value
    return {"services": {"main": main}} if main else {}


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


def _gpu_for(role: str, gpu_role: str, gpus: Mapping[str, str], configured: Any) -> str:
    """這個角色最後要用哪張卡。優先序:`--<role>-gpu` > `--aux-gpu` > 檔案 > 不指定。

    `--aux-gpu` 只套到三個附屬角色(main 有自己的 `--main-gpu`)。回空字串就是
    「不指定」—— `build_server_command` 不會加 `env CUDA_VISIBLE_DEVICES=` 前綴,
    而 pane 最終環境已經把繼承來的那一份剝掉了。
    """
    value = gpus.get(role) or (gpus.get("aux") if gpu_role == "aux" else "") or configured or ""
    return _validate_gpu_selector(value, f"GPU selector for {role}")


def _llama_bin(env: Mapping[str, str], local_data: Mapping[str, Any] | None, override: str | None) -> str:
    """llama-server 執行檔:argv > `deployment.json` 的 `llama_bin` > `DEFAULT_LLAMA_BIN`。"""
    if override:
        return _validate_llama_bin(override, "--llama-bin")
    configured = (local_data or {}).get("llama_bin")
    if configured:
        return _validate_llama_bin(configured, "deployment override llama_bin")
    home = _home(env)
    if home and DEFAULT_LLAMA_BIN.startswith("~/"):
        return str(Path(home) / DEFAULT_LLAMA_BIN[2:])
    return str(_expanduser(DEFAULT_LLAMA_BIN, "llama_bin"))


def load_effective_profile(
    environ: Mapping[str, str] | None = None,
    *,
    profile: str | None = None,
    overrides: LauncherOverrides | None = None,
    deployment_config: str | Path | None = None,
    model_registry_file: str | Path | None = None,
) -> DeploymentProfile:
    """Load defaults < selected profile < local override < launcher argv overrides.

    `environ` 只被讀 `HOME` / `USERPROFILE`(檔案在哪);設定值一律來自檔案與
    `overrides`。`deployment_config` / `model_registry_file` 是 set_config 驗證暫存檔
    用的明確路徑,不是使用者設定。
    """
    env = os.environ if environ is None else environ
    overrides = overrides or LauncherOverrides()

    override_path = local_override_path(env, deployment_config=deployment_config)
    local_data: dict[str, Any] | None = None
    if override_path and override_path.is_file():
        local_data = _read_json_object(override_path, "local deployment override")
        _validate_document(local_data, f"local deployment override {override_path}", local=True)
    elif override_path and deployment_config:
        raise ProfileError(
            f"--deployment-config must point to an existing file: {override_path}"
        )

    selected = (profile or "").strip()
    if not selected and local_data:
        selected = str(local_data.get("profile") or "").strip()
    selected = selected or "defaults"

    data, selected_name = _load_profile_chain(selected)
    if local_data:
        data = _merge(data, {"services": local_data.get("services", {})})
    data = _merge(data, _overrides_overlay(overrides))
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
            gpu=_gpu_for(role, raw["gpu_role"], overrides.gpus, raw.get("gpu")),
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
        selected_profile=selected_name,
        local_override=override_path if local_data else None,
        llama_bin=_llama_bin(env, local_data, overrides.llama_bin),
        registry_file=Path(model_registry_file) if model_registry_file else None,
    )


def load_model_registry(
    environ: Mapping[str, str] | None = None,
    *,
    registry_file: str | Path | None = None,
) -> dict[str, str]:
    """bare name → GGUF 絕對路徑。來源只有一個檔。

    `registry_file` 是呼叫端明確交來的路徑(set_config 驗證用的暫存檔);沒給就是
    `~/.config/codetrail/models.json`。以前這裡還認兩個環境變數,那讓另一份安裝
    的殼層可以決定「這個 bare name 指到哪一顆 GGUF」。
    """
    env = environ if environ is not None else os.environ
    if registry_file:
        path = _expanduser(_reject_control(str(registry_file).strip(), "--model-registry-file"), "--model-registry-file")
    else:
        home = _home(env)
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
    registry_file: str | Path | None = None,
) -> str:
    if reference is None:
        raise ProfileError(
            "main model is unset; put a registry key or an absolute GGUF path in "
            "deployment.json services.main.model(重跑 ./set_config.sh 也會寫好它)"
        )
    env = environ if environ is not None else os.environ
    ref = _validate_model_reference(reference, "model reference", nullable=False)
    assert ref is not None
    expanded = _expanduser(ref, "model reference")
    if expanded.is_absolute():
        path = expanded
    else:
        registry = load_model_registry(env, registry_file=registry_file)
        registered = registry.get(ref)
        if registered:
            path = Path(registered)
        elif ref in _LEGACY_MODEL_PATHS:
            # 舊 launcher 的目錄配置。models.json 沒有這個鍵時的最後手段,
            # 位置固定在 `~/models`(以前還有一個 MODELS_DIR 環境變數)。
            directory, filename, pattern = _LEGACY_MODEL_PATHS[ref]
            home = _home(env)
            models_dir = Path(home) / "models" if home else _expanduser("~/models", "legacy models dir")
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
    這必須是使用者的明確選擇(deployment.json 的 bind 欄位,或 set_config 的
    --allow-remote),不能是 localhost 的靜默轉譯。
    """
    host = urlsplit(service.base_url).hostname or ""
    if host == "localhost" or host.startswith("127.") or host == "::1":
        return "0.0.0.0" if service.bind == "all-interfaces" else "127.0.0.1"
    return host


def cpu_moe_disables_fit(parameters: Mapping[str, Any]) -> bool:
    """CPU-MoE 是否讓這個 role 的 --fit 失去作用。

    llama.cpp 的 common_params_fit_impl 一看到 model_params::tensor_buft_overrides
    已被使用者設定就 abort(只印一行 WARN 就繼續載入),而 --cpu-moe / --n-cpu-moe
    正是往那裡塞 override。n_cpu_moe: 0 不產生任何 override(common/arg.cpp),
    所以不算。
    """
    return bool(parameters.get("cpu_moe") or parameters.get("n_cpu_moe"))


def cpu_moe_fit_conflict(service: ServiceProfile) -> str | None:
    """回傳「設定寫了但不會生效」的說明;沒有衝突回 None。

    刻意不在 schema 層拒絕:config.py 在 import 期就載入 effective profile,
    硬拒會讓整個 CodeTrail(含 MCP server)無法啟動,而這個組合 llama.cpp 自己
    是容忍的。改成在 build_server_command 剔除不會生效的旗標 + 由 launcher 提醒。
    """
    p = service.parameters
    if not cpu_moe_disables_fit(p):
        return None
    claims = []
    # --fit 的預設值是 "on":省略 fit 與明寫 fit "on" 一樣會走進 fit 然後 abort,
    # 兩者都會被 build_server_command 改寫成 --fit off,所以都要說。
    if p.get("fit", "on") != "off":
        claims.append(
            'fit "on"(明寫)' if "fit" in p else "fit 未設定(llama.cpp 預設即 on)"
        )
    if "fit_target" in p:
        claims.append(f"fit_target {p['fit_target']}(不會被保留)")
    if p.get("gpu_layers") == "auto":
        claims.append('gpu_layers "auto"(fit 不跑時等同「全部層上 GPU」)')
    if not claims:
        return None
    return (
        f"services.{service.role} 同時設了 CPU-MoE 與 " + "、".join(claims)
        + ":llama.cpp 會因為 tensor override 而放棄 --fit,這些值不會生效。"
        "啟動指令已自動改寫成 --fit off 並剔除 --fit-target;"
        "要讓設定檔與實際行為一致請重跑 ./set_config.sh。"
    )


def warn_cpu_moe_fit_conflicts(
    services: Sequence[ServiceProfile], *, prefix: str = "[!]"
) -> None:
    """把「設定檔寫了但不會生效」印到 stderr。

    每一條真的會啟動 server 的路徑都要呼叫(launch_servers 與
    `deployment_profile.py exec` 的 systemd 路徑),否則就變成靜默矯正。
    """
    for service in services:
        conflict = cpu_moe_fit_conflict(service)
        if conflict:
            print(f"{prefix} ⚠ {conflict}", file=sys.stderr)


def build_server_command(
    service: ServiceProfile,
    llama_bin: str,
    environ: Mapping[str, str] | None = None,
    *,
    must_exist: bool = False,
    registry_file: str | Path | None = None,
) -> list[str]:
    """Build argv only from validated structured fields and the parameter allowlist."""
    model_path = resolve_model_reference(
        service.model, environ, must_exist=must_exist, registry_file=registry_file
    )
    command = [llama_bin, "-m", model_path]
    if service.mmproj:
        command.extend([
            "--mmproj",
            resolve_model_reference(
                service.mmproj, environ, must_exist=must_exist, registry_file=registry_file
            ),
        ])
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
    if p.get("cpu_moe"):
        command.append("--cpu-moe")
    # CPU-MoE 之下強制 --fit off。--fit 的預設值是 "on",不輸出等同 on,一樣會
    # 走進 common_params_fit_impl 然後因 tensor override 而 abort(多一次無用嘗試
    # 加一行嚇人的 WARN);明寫 off 才會真的跳過。fit_target 在 fit off 之下無意義,
    # 一併不輸出 —— 既有設定檔不必重跑 set_config 也立刻拿到正確的啟動指令。
    skip: set[str] = set()
    if cpu_moe_disables_fit(p):
        skip = {"fit", "fit_target"}
        command.extend(["--fit", "off"])
    parameter_flags = (
        ("temperature", "--temp"),
        ("top_p", "--top-p"),
        ("top_k", "--top-k"),
        ("min_p", "--min-p"),
        ("presence_penalty", "--presence-penalty"),
        ("cache_type_k", "--cache-type-k"),
        ("cache_type_v", "--cache-type-v"),
        ("cache_ram", "--cache-ram"),
        ("n_cpu_moe", "--n-cpu-moe"),
        ("fit", "--fit"),
        ("fit_target", "--fit-target"),
        ("parallel", "-np"),
        ("flash_attention", "-fa"),
        ("threads", "-t"),
    )
    for key, flag in parameter_flags:
        if key in p and key not in skip:
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
            item["model_path"] = resolve_model_reference(
                service.model, environ, registry_file=profile.registry_file
            )
        except ProfileError:
            item["model_path"] = None
        if service.mmproj:
            try:
                item["mmproj_path"] = resolve_model_reference(
                    service.mmproj, environ, registry_file=profile.registry_file
                )
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
        "llama_bin": profile.llama_bin,
        "services": services,
    }


#: `--<name>-gpu` → `LauncherOverrides.gpus` 的鍵。`aux` 套到三個附屬角色。
_GPU_FLAGS = (
    ("--main-gpu", "main_gpu", "main"),
    ("--aux-gpu", "aux_gpu", "aux"),
    ("--embed-gpu", "embed_gpu", "embedding"),
    ("--rerank-gpu", "rerank_gpu", "reranker"),
    ("--vl-gpu", "vl_gpu", "vl"),
)
#: `--main-<field>` → `LauncherOverrides` 的欄位。
_MAIN_FLAGS = (
    ("--main-model", "main_model", str),
    ("--main-ctx", "main_ctx", int),
    ("--main-batch", "main_batch", int),
    ("--main-ubatch", "main_ubatch", int),
)


def add_loader_arguments(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    """把 loader 的全部 argv 掛到一個 parser 上。

    launcher / stop / status / `deployment_profile.py` 自己都用這一份 —— 旗標分叉
    就是「launcher 用 A、status 檢查 B」。`suppress_defaults` 給子命令用:argparse
    的 subparser 會把自己的預設值寫回同一個 namespace,不 SUPPRESS 的話
    `--profile X exec main` 會被子命令的 `None` 蓋掉。
    """
    extra: dict[str, Any] = {"default": argparse.SUPPRESS} if suppress_defaults else {}
    parser.add_argument("--profile", help='"defaults" or absolute JSON profile path', **extra)
    # 這兩個是行程之間交暫存檔用的(set_config 驗證尚未寫入的設定),不是使用者旗標。
    parser.add_argument("--deployment-config", help=argparse.SUPPRESS, **extra)
    parser.add_argument("--model-registry-file", help=argparse.SUPPRESS, **extra)
    parser.add_argument("--llama-bin", help="llama-server 執行檔(絕對路徑;預設讀 deployment.json)", **extra)
    for flag, _dest, caster in _MAIN_FLAGS:
        parser.add_argument(flag, type=caster, help=f"覆寫 main 的 {flag[7:]}", **extra)
    for flag, _dest, role in _GPU_FLAGS:
        parser.add_argument(flag, help=f"{role} 角色的 GPU selector", **extra)


def loader_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    """`add_loader_arguments` 解析出來的 namespace → `load_effective_profile` 的 kwargs。"""
    gpus = {
        role: str(getattr(args, dest))
        for _flag, dest, role in _GPU_FLAGS
        if getattr(args, dest, None)
    }
    overrides = LauncherOverrides(
        main_model=getattr(args, "main_model", None),
        main_ctx=getattr(args, "main_ctx", None),
        main_batch=getattr(args, "main_batch", None),
        main_ubatch=getattr(args, "main_ubatch", None),
        gpus=gpus,
        llama_bin=getattr(args, "llama_bin", None),
    )
    return {
        "profile": getattr(args, "profile", None),
        "overrides": overrides,
        "deployment_config": getattr(args, "deployment_config", None),
        "model_registry_file": getattr(args, "model_registry_file", None),
    }


def loader_argv(args: argparse.Namespace) -> list[str]:
    """反向:同一組值變回 argv。

    launcher 要把「自己收到的設定」原封不動交給 pane 裡的 `exec`;重新讀一次檔案
    不等價(argv 覆寫會消失),重新組一份手寫清單則會漂移。
    """
    out: list[str] = []
    for flag, dest in (
        ("--profile", "profile"),
        ("--deployment-config", "deployment_config"),
        ("--model-registry-file", "model_registry_file"),
        ("--llama-bin", "llama_bin"),
    ):
        value = getattr(args, dest, None)
        if value:
            out.extend([flag, str(value)])
    for flag, dest, _caster in _MAIN_FLAGS:
        value = getattr(args, dest, None)
        if value is not None:
            out.extend([flag, str(value)])
    for flag, dest, _role in _GPU_FLAGS:
        value = getattr(args, dest, None)
        if value:
            out.extend([flag, str(value)])
    return out


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve and validate CodeTrail deployment profiles")
    add_loader_arguments(parser)
    sub = parser.add_subparsers(dest="command", required=True)
    # loader 旗標同時掛在子命令上:pane 的命令是 `exec <role> <loader argv>`
    # (角色在前,設定在後),systemd 的 ExecStart 也照這個形狀寫。
    for name, help_text in (
        ("show", "print the effective profile as JSON"),
        ("validate", "validate profile and optionally require model files"),
        ("get", "print one effective service field"),
        ("exec", "exec one role directly (for systemd or another supervisor)"),
    ):
        child = sub.add_parser(name, help=help_text)
        add_loader_arguments(child, suppress_defaults=True)
        if name == "validate":
            child.add_argument("--require-files", action="store_true")
        elif name == "get":
            child.add_argument("role", choices=ROLES)
            child.add_argument(
                "field",
                choices=("model", "mmproj", "port", "base_url", "bind", "gpu_role", "gpu", "ctx", "batch", "ubatch"),
            )
        elif name == "exec":
            child.add_argument("role", choices=ROLES)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        kwargs = loader_kwargs(args)
        profile = load_effective_profile(**kwargs)
        if args.command == "show":
            print(json.dumps(profile_as_dict(profile), ensure_ascii=False, indent=2, sort_keys=True))
        elif args.command == "validate":
            if args.require_files:
                for service in profile.services.values():
                    resolve_model_reference(
                        service.model, must_exist=True, registry_file=profile.registry_file
                    )
                    if service.mmproj:
                        resolve_model_reference(
                            service.mmproj, must_exist=True, registry_file=profile.registry_file
                        )
            print(f"profile={profile.selected_profile} verification={profile.verification} valid")
        elif args.command == "get":
            value = getattr(profile.service(args.role), args.field)
            print("" if value is None else value)
        elif args.command == "exec":
            service = profile.service(args.role)
            # systemd 之類的 supervisor 只會走這裡:少了這行就等於靜默矯正。
            warn_cpu_moe_fit_conflicts([service], prefix="[deployment-profile]")
            command = build_server_command(
                service,
                profile.llama_bin,
                must_exist=True,
                registry_file=profile.registry_file,
            )
            # 這是 llama-server 唯一真正被 exec 的地方,所以也是最終環境的唯一決定點:
            # 剝掉 CodeTrail 的四個前綴 + `LLAMA_ARG_*` + `CUDA_VISIBLE_DEVICES`。
            # GPU 只由 command 前面那個 `env CUDA_VISIBLE_DEVICES=<驗證過的值>` 重新輸出。
            os.execvpe(command[0], command, process_env.llama_server_env())
        return 0
    except (OSError, ProfileError) as exc:
        print(f"[deployment-profile] ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
