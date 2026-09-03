#!/usr/bin/env python3
"""Mine private OpenCode sessions and run evidence-first model comparisons.

This is a manual, explicitly authorised eval lane.  It is not part of pytest,
CI, ``aicode_opencode`` startup, or the checked-in NDA-safe fixtures under ``eval/``.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import compaction_mode  # noqa: E402
import deployment_profile  # noqa: E402
import model_resolution  # noqa: E402
import root_safety  # noqa: E402
import session_eval  # noqa: E402
from scripts.eval_tool_routing import (  # noqa: E402
    EvalError,
    LocalJsonClient,
    parse_event_stream,
    read_opencode_version,
)

DEFAULT_PRIVATE_DIR = REPO_ROOT / ".codetrail" / "session_eval"
# The compaction section CodeTrail may own in the global config.  Replay drops
# it together with the plugin so the two never disagree (see _evaluation_config).
COMPACTION_SECTION = compaction_mode.COMPACTION_SECTION
# Must match `STATE_PATH_ENV` in opencode_plugins/codetrail-compaction.js.
COMPACTION_STATE_ENV = "AICODE_COMPACTION_STATE"
# Must match `COMPACTION_VERSION_ENV` in scripts/opencode_direct_contract.py.
COMPACTION_VERSION_ENV = "AICODE_OPENCODE_VERSION"
MAX_SUBPROCESS_OUTPUT_BYTES = 64 * 1024 * 1024
DEFAULT_TURN_TIMEOUT = 600
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_SAFE_CANDIDATE_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]{2,79}$")

# Defense in depth for replay against a real private project.  OpenCode denies
# these exact tools after the broad codetrail_* allow rule, while the MCP server
# separately receives AI_CODE_PATCH=0 / AI_CODE_RUN_TESTS=0.
MUTATING_FRONTEND_TOOLS = (
    "codetrail_apply_patch",
    "codetrail_run_lint",
    "codetrail_run_command",
    "codetrail_ingest_document",
    "codetrail_remove_document",
    "codetrail_review_figures",
    "codetrail_import_external_file",
    "codetrail_record_lesson",
)


class _CommandTimedOut(session_eval.SessionEvalError):
    """A killed subprocess whose bounded partial output is still usable."""

    def __init__(self, command: str, timeout: int, stdout: bytes, stderr: bytes):
        super().__init__(f"command timed out after {timeout}s: {command}")
        self.stdout = stdout
        self.stderr = stderr


def _print(message: str, *, error: bool = False) -> None:
    print(f"[session-eval] {message}", file=sys.stderr if error else sys.stdout, flush=True)


def _bounded_run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise session_eval.SessionEvalError(f"command is unavailable: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, bytes) else b""
        stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
        if len(stdout) > MAX_SUBPROCESS_OUTPUT_BYTES or len(stderr) > MAX_SUBPROCESS_OUTPUT_BYTES:
            raise session_eval.SessionEvalError(
                f"timed-out command output exceeded the private eval limit: {command[0]}"
            ) from exc
        raise _CommandTimedOut(command[0], timeout, stdout, stderr) from exc
    if len(completed.stdout) > MAX_SUBPROCESS_OUTPUT_BYTES or len(completed.stderr) > MAX_SUBPROCESS_OUTPUT_BYTES:
        raise session_eval.SessionEvalError(f"command output exceeded the private eval limit: {command[0]}")
    return completed


def _load_suite(path: Path) -> dict[str, Any]:
    return session_eval.validate_suite(session_eval.read_json_file(path))


def _export_one(session_id: str, output_dir: Path, *, sanitized: bool) -> Path:
    if not _SAFE_SESSION_ID_RE.fullmatch(session_id):
        raise session_eval.SessionEvalError("session id is invalid")
    command = ["opencode", "export", session_id]
    if sanitized:
        command.append("--sanitize")
    completed = _bounded_run(command, cwd=REPO_ROOT, env=os.environ, timeout=120)
    if completed.returncode != 0:
        raise session_eval.SessionEvalError("opencode export failed")
    try:
        value = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise session_eval.SessionEvalError("opencode export returned invalid JSON") from exc
    exported = session_eval.validate_opencode_export(value)
    if exported["info"]["id"] != session_id:
        raise session_eval.SessionEvalError("opencode export returned a different session id")
    digest = session_eval.session_hash(session_id)
    return session_eval.write_private_json(output_dir, f"export-{digest}.json", exported)


def command_export(args: argparse.Namespace) -> int:
    sessions = list(dict.fromkeys(args.session or []))
    if not sessions:
        raise session_eval.SessionEvalError("export needs at least one --session")
    if len(sessions) > session_eval.MAX_SESSIONS:
        raise session_eval.SessionEvalError("too many sessions requested")
    output_dir = args.output_dir.expanduser()
    for session_id in sessions:
        _export_one(session_id, output_dir, sanitized=args.sanitize)
    _print(f"exported {len(sessions)} session(s) into private storage")
    return 0


def command_mine(args: argparse.Namespace) -> int:
    source_dir = args.source_dir.expanduser()
    if source_dir.is_symlink() or not source_dir.is_dir():
        raise session_eval.SessionEvalError("mine source directory must be a regular directory")
    paths = sorted(source_dir.glob(args.glob))
    corpus = session_eval.mine_export_files(paths)
    output = session_eval.write_private_json(args.output_dir, args.output_name, corpus)
    manual = sum(
        bool(draft["mining"]["requires_manual_curation"]) for draft in corpus["drafts"]
    )
    _print(f"mined {len(corpus['drafts'])} session(s); {manual} need manual neutralization")
    _print(f"draft corpus: {output}")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    suite = _load_suite(args.suite)
    kinds: dict[str, int] = {}
    for case in suite["cases"]:
        kind = case["verifier"]["oracle_kind"]
        kinds[kind] = kinds.get(kind, 0) + 1
    _print(
        f"suite valid: {len(suite['cases'])} cases; digest={session_eval.suite_digest(suite)[:16]}; "
        + ", ".join(f"{key}={value}" for key, value in sorted(kinds.items()))
    )
    return 0


def _validate_project_root(raw: str) -> Path:
    resolved, error = root_safety.validate_aicode_root(
        raw,
        os.environ.get("HOME") or os.environ.get("USERPROFILE"),
        allow_home_override=False,
    )
    if error or not resolved:
        raise session_eval.SessionEvalError("suite project_root is not a safe AICODE_ROOT")
    return Path(resolved)


def _hash_command_state(command: Sequence[str], *, root: Path, digest: hashlib._Hash) -> None:
    completed = _bounded_run(command, cwd=root, env=os.environ, timeout=120)
    digest.update(command[1].encode("utf-8") if len(command) > 1 else b"")
    digest.update(str(completed.returncode).encode("ascii"))
    digest.update(completed.stdout)


def project_state_digest(root: Path, state_paths: Sequence[str]) -> str:
    """Hash reproducibility state without serialising paths or file contents."""

    digest = hashlib.sha256()
    git_probe = _bounded_run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        env=os.environ,
        timeout=30,
    )
    if git_probe.returncode == 0 and git_probe.stdout.strip() == b"true":
        for command in (
            ("git", "rev-parse", "HEAD"),
            ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
            ("git", "diff", "--no-ext-diff", "--binary", "HEAD"),
        ):
            _hash_command_state(command, root=root, digest=digest)
    else:
        digest.update(b"not-a-git-worktree")

    root_real = root.resolve()
    for raw in sorted(state_paths):
        relative = Path(raw)
        target = root / relative
        try:
            target_real = target.resolve(strict=False)
        except OSError as exc:
            raise session_eval.SessionEvalError("cannot resolve a suite state path") from exc
        if target_real != root_real and root_real not in target_real.parents:
            raise session_eval.SessionEvalError("suite state path escapes project_root")
        digest.update(raw.encode("utf-8"))
        try:
            st = target.stat(follow_symlinks=False)
        except FileNotFoundError:
            digest.update(b"missing")
            continue
        except OSError as exc:
            raise session_eval.SessionEvalError("cannot inspect a suite state path") from exc
        if target.is_symlink() or not stat.S_ISREG(st.st_mode):
            raise session_eval.SessionEvalError("suite state paths must be regular non-symlink files")
        digest.update(str(st.st_size).encode("ascii"))
        try:
            with target.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise session_eval.SessionEvalError("cannot hash a suite state path") from exc
    return digest.hexdigest()


def _global_config() -> tuple[Path, dict[str, Any]]:
    path, value, error = model_resolution.load_first_opencode_config(os.environ)
    if error or path is None or not isinstance(value, dict):
        raise session_eval.SessionEvalError("cannot load the OpenCode config for replay")
    return path, value


def _evaluation_config(
    value: Mapping[str, Any], model: str, *, keep_compaction: bool = False
) -> dict[str, Any]:
    provider, separator, model_id = model.partition("/")
    if not separator or not provider or not model_id:
        raise session_eval.SessionEvalError("--model must use provider/model syntax")
    config = copy.deepcopy(dict(value))
    providers = config.get("provider")
    provider_spec = providers.get(provider) if isinstance(providers, dict) else None
    models = provider_spec.get("models") if isinstance(provider_spec, dict) else None
    if not isinstance(models, dict) or model_id not in models:
        raise session_eval.SessionEvalError("candidate model is absent from OpenCode config")
    config["model"] = model
    permission = config.get("permission")
    if not isinstance(permission, dict):
        permission = {}
        config["permission"] = permission
    for tool in MUTATING_FRONTEND_TOOLS:
        permission[tool] = "deny"
    for builtin in ("bash", "read", "grep", "glob", "edit", "write", "apply_patch", "task"):
        permission[builtin] = "deny"
    # Notifications and project-relative lesson files are not part of a frozen
    # replay contract.  The global AGENTS instructions still apply uniformly.
    config["plugin"] = []
    config["instructions"] = []
    if keep_compaction:
        # The one suite whose subject *is* compaction keeps both halves: the
        # managed ``compaction.*`` values and the plugin that acts on them.
        # Anything less is the hybrid described below, so a missing plugin file
        # is a hard error rather than a silent downgrade to one half.
        if not compaction_mode.PLUGIN_PATH.is_file():
            raise session_eval.SessionEvalError(
                "--keep-compaction needs the compaction plugin, but "
                f"{compaction_mode.PLUGIN_PATH} is missing"
            )
        section = config.get(COMPACTION_SECTION)
        # Only the contract keys are required: those are the ones compaction
        # cannot happen without.  ``prune`` is managed too, but a config written
        # before it became managed still compacts correctly, so demanding it
        # would refuse a configuration the user really runs.
        missing = [
            key for key in compaction_mode.CONTRACT_COMPACTION_KEYS
            if not isinstance(section, dict) or key not in section
        ]
        if missing:
            raise session_eval.SessionEvalError(
                "--keep-compaction needs the managed compaction settings in the "
                f"global config; missing: {', '.join(missing)}. "
                "Re-run ./set_config.sh to write them."
            )
        config["plugin"] = [str(compaction_mode.PLUGIN_PATH)]
        return config
    # Dropping the plugin without dropping the settings it was written for
    # leaves a hybrid nobody runs interactively: upstream auto-compaction
    # disabled (``compaction.auto = false`` also disables mid-turn compaction
    # and provider-overflow replay) with nothing left to compact at idle.  A
    # long case would turn into a provider error that never happens in the real
    # client, and ``compaction_events`` would silently read zero.
    #
    # Only CodeTrail's own keys go.  ``reserved`` is upstream schema the user may
    # have set independently; replacing it with the upstream default would
    # measure a configuration the user does not run.  ``prune`` *is* one of ours
    # (see ``compaction_mode.MANAGED_COMPACTION_KEYS``), so it goes with the rest
    # -- a replay without the plugin must also drop the tool-output pruning that
    # only CodeTrail's takeover turns on.
    section = config.get(COMPACTION_SECTION)
    if isinstance(section, dict):
        for key in compaction_mode.MANAGED_COMPACTION_KEYS:
            section.pop(key, None)
        if not section:
            config.pop(COMPACTION_SECTION, None)
    return config


def _require_compaction_runtime(
    config: Mapping[str, Any], model: str, opencode_version: str | None
) -> None:
    """`--keep-compaction` 必須真的能壓縮,否則結果是「沒有壓縮」而沒人知道。

    兩件事會讓 plugin 在第一次 idle 就停用,而 eval 照常把 `compaction_events=0`
    寫成結果:候選模型的有效限制推導不出設定裡那組受管值(global 是用另一個
    模型的 ctx 算的),以及 OpenCode 版本低於壓縮語意的下限。
    """
    minimum = compaction_mode.MIN_COMPACTION_OPENCODE_VERSION
    parsed = None
    if isinstance(opencode_version, str):
        parts = opencode_version.split(".")
        if len(parts) >= 3 and all(part.isdigit() for part in parts[:3]):
            parsed = tuple(int(part) for part in parts[:3])
    if parsed is None or parsed < minimum:
        raise session_eval.SessionEvalError(
            "--keep-compaction needs OpenCode >= "
            f"{'.'.join(map(str, minimum))}; measured {opencode_version!r}"
        )
    # 上游用 `agent.compaction.model` 做 tail selection 與摘要;驗候選模型
    # 而不驗它,runtime 會照樣以另一個模型重算並停用。反過來只驗它也不行 ——
    # 觸發之前那段對話壓的是候選模型,受管值是兩者的合併值
    # (compaction_mode.combine_settings,writer / plugin / doctor 走同一條)。
    def limit_of(ref: str):
        provider_id, _, model_id = ref.partition("/")
        providers = config.get("provider")
        provider = providers.get(provider_id) if isinstance(providers, Mapping) else None
        models = provider.get("models") if isinstance(provider, Mapping) else None
        entry = models.get(model_id) if isinstance(models, Mapping) else None
        found = entry.get("limit") if isinstance(entry, Mapping) else None
        return found if isinstance(found, Mapping) else None

    agent = config.get("agent")
    compaction_agent = agent.get("compaction") if isinstance(agent, Mapping) else None
    configured = (
        compaction_agent.get("model") if isinstance(compaction_agent, Mapping) else None
    )
    live_model = model
    summariser_model = (
        configured if isinstance(configured, str) and "/" in configured else model
    )
    section = config.get(compaction_mode.COMPACTION_SECTION)
    reserved = section.get("reserved") if isinstance(section, Mapping) else None
    per_model = {}
    for ref in dict.fromkeys((summariser_model, live_model)):
        limit = limit_of(ref)
        if limit is None:
            raise session_eval.SessionEvalError(
                f"--keep-compaction needs limit.context/limit.output for {ref}"
            )
        try:
            per_model[ref] = compaction_mode.derive_settings(
                context_limit=limit.get("context"),
                output_limit=limit.get("output"),
                input_limit=limit.get("input"),
                reserved=reserved,
            )
        except compaction_mode.CompactionModeError as exc:
            raise session_eval.SessionEvalError(
                f"--keep-compaction cannot derive a compaction threshold for {ref}: {exc}"
            ) from exc
    derived = per_model[summariser_model]
    label = summariser_model
    if summariser_model != live_model:
        label = f"{summariser_model} + {live_model}"
        try:
            derived = compaction_mode.combine_settings(derived, per_model[live_model])
        except compaction_mode.CompactionModeError as exc:
            raise session_eval.SessionEvalError(
                f"--keep-compaction cannot derive a compaction threshold for {label}: {exc}"
            ) from exc
    # Only the contract keys.  This preflight exists to predict whether the
    # plugin will disable itself at the first idle, and that decision is made
    # over ``CONTRACT_COMPACTION_KEYS`` alone (compaction_mode.effective_drift,
    # and the JS side does the same).  Comparing the full ``config_values``
    # would also demand ``prune``, which ``_evaluation_config`` deliberately
    # accepts as absent -- a config written before ``prune`` became managed
    # compacts correctly, and so does one where the user turned it off.  Two
    # layers giving opposite answers about the same config is the bug: the
    # replay never starts, and the "works with older settings" promise is void.
    expected_values = {
        key: derived.config_values[key]
        for key in compaction_mode.CONTRACT_COMPACTION_KEYS
    }
    mismatched = [
        key for key, expected in expected_values.items()
        if not isinstance(section, Mapping)
        or not compaction_mode.json_equal(section.get(key), expected)
    ]
    if mismatched:
        raise session_eval.SessionEvalError(
            "--keep-compaction: the managed compaction values were derived for a "
            f"different model; {label} needs {expected_values} but the config "
            f"has {dict(section) if isinstance(section, Mapping) else None} "
            f"(mismatched: {', '.join(mismatched)})"
        )


def _compaction_identity(config: Mapping[str, Any], keep_compaction: bool) -> dict[str, Any]:
    """The compaction semantics this replay runs under.

    Two runs that differ here are not comparable and must not share a resume
    checkpoint: one may compact at idle under CodeTrail's rules while the other
    runs upstream defaults, and a suite half-collected under each would be
    silently merged.
    """
    section = config.get(compaction_mode.COMPACTION_SECTION)
    agent = config.get("agent")
    identity: dict[str, Any] = {
        "keep_compaction": bool(keep_compaction),
        "section": section if isinstance(section, Mapping) else None,
        "plugin": list(config.get("plugin") or []),
        # `agent.compaction` 決定摘要用哪個模型、什麼溫度、什麼 system prompt。
        # 中途換掉它,兩半結果不可比 —— 一般 replay 也一樣(它跑的是上游原生
        # 壓縮,而那同樣會用這個 agent)。
        "agent": _compaction_agent_identity(agent),
    }
    if keep_compaction:
        identity["plugin_digest"] = _file_digest(compaction_mode.PLUGIN_PATH)
        identity["rules_digest"] = _file_digest(compaction_mode.RULES_DOC)
    return identity


_FILE_PROMPT_REFERENCE_RE = re.compile(r"^\{file:(.+)\}$", re.DOTALL)


def _compaction_agent_identity(agent: Any) -> dict[str, Any] | None:
    """`agent.compaction` 的身分,`{file:...}` 解析成內容雜湊。

    只存原始 mapping 的話,同一個路徑下的 prompt 內容被換掉,fingerprint 不變,
    兩種摘要 system prompt 的結果會併進同一個 checkpoint。canary 早就這樣做了
    (`tool_call_canary._effective_agent_prompt_digest`),這裡沿用同一條規則。
    """
    entry = agent.get("compaction") if isinstance(agent, Mapping) else None
    if not isinstance(entry, Mapping):
        return None
    identity = {key: entry[key] for key in sorted(entry) if key != "prompt"}
    prompt = entry.get("prompt")
    if isinstance(prompt, str):
        match = _FILE_PROMPT_REFERENCE_RE.fullmatch(prompt.strip())
        if match is None:
            identity["prompt"] = {"kind": "inline", "digest": session_eval.text_digest(prompt)}
        else:
            identity["prompt"] = {
                "kind": "file",
                "digest": _file_digest(Path(match.group(1)).expanduser()),
            }
    elif prompt is not None:
        identity["prompt"] = {"kind": "unknown"}
    return identity


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unavailable"


def _write_replay_compaction_state(
    directory: Path, config: Mapping[str, Any], config_path: Path
) -> Path:
    """Bind a throw-away ownership state to the replay config.

    Only ``--keep-compaction`` uses this.  It never touches the real
    ``~/.config/codetrail/compaction.json``: that file describes the user's
    actual OpenCode config and must keep describing it.
    """
    section = config.get(compaction_mode.COMPACTION_SECTION)
    if not isinstance(section, Mapping):
        raise session_eval.SessionEvalError(
            "--keep-compaction needs managed compaction settings in the global config"
        )
    managed = {
        key: {"prior": {"present": False}, "value": section[key]}
        for key in compaction_mode.MANAGED_COMPACTION_KEYS
        if key in section
    }
    if any(key not in managed for key in compaction_mode.CONTRACT_COMPACTION_KEYS):
        raise session_eval.SessionEvalError(
            "--keep-compaction needs every contract compaction key in the global "
            "config; re-run ./set_config.sh to write them"
        )
    state = compaction_mode.build_state(
        mode=compaction_mode.MODE_CODETRAIL,
        config_path=config_path,
        managed=managed,
        plugin={"registered": True, "prior_present": False, "entry": "string"},
        section_present=True,
    )
    return compaction_mode.save_state(state, path=directory / "compaction-replay.json")


def _write_temp_config(directory: Path, value: Mapping[str, Any]) -> Path:
    path = directory / "opencode-eval.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise session_eval.SessionEvalError("cannot create temporary replay config") from exc
    return path


def _normalise_base_url(config: Mapping[str, Any], model: str, env: Mapping[str, str]) -> str:
    provider = model.split("/", 1)[0]
    providers = config.get("provider")
    provider_spec = providers.get(provider) if isinstance(providers, Mapping) else None
    options = provider_spec.get("options") if isinstance(provider_spec, Mapping) else None
    configured = options.get("baseURL") if isinstance(options, Mapping) else None
    base = env.get("AICODE_LLAMA_BASE_URL") or configured or "http://localhost:8080"
    if not isinstance(base, str) or not base.strip():
        raise session_eval.SessionEvalError("candidate model endpoint is invalid")
    base = base.rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def _path_from_props(props: Mapping[str, Any]) -> str:
    value = props.get("model_path")
    if isinstance(value, str) and value:
        return value
    settings = props.get("default_generation_settings")
    nested = settings.get("model") if isinstance(settings, Mapping) else None
    return nested if isinstance(nested, str) else ""


def _same_model_artifact(expected: Path, actual_raw: str) -> bool:
    if not actual_raw:
        return False
    actual = Path(actual_raw).expanduser()
    try:
        if expected.is_file() and actual.is_file():
            return os.path.samefile(expected, actual)
    except OSError:
        pass
    try:
        return expected.resolve(strict=False) == actual.resolve(strict=False)
    except OSError:
        return False


def _candidate_identity(
    *,
    model: str,
    config: Mapping[str, Any],
    env: Mapping[str, str],
    keep_compaction: bool = False,
    opencode_version: str | None = None,
) -> dict[str, Any]:
    bare = model.split("/", 1)[1]
    expected_path = Path(
        deployment_profile.resolve_model_reference(bare, env, must_exist=True)
    )
    client = LocalJsonClient(_normalise_base_url(config, model, env), timeout_seconds=120)
    props = client.get_json("/props")
    if not _same_model_artifact(expected_path, _path_from_props(props)):
        raise session_eval.SessionEvalError(
            "loaded llama-server artifact does not match the selected candidate model"
        )
    settings = props.get("default_generation_settings")
    settings = settings if isinstance(settings, Mapping) else {}
    build_info = props.get("build_info")
    identity = {
        "selected_model": model,
        "artifact_digest": session_eval.text_digest(str(expected_path.resolve(strict=False))),
        "chat_template_digest": (
            session_eval.text_digest(props["chat_template"])
            if isinstance(props.get("chat_template"), str)
            else None
        ),
        "chat_template_caps": (
            dict(props["chat_template_caps"])
            if isinstance(props.get("chat_template_caps"), Mapping)
            else None
        ),
        "llama_cpp_build_digest": session_eval.json_digest(build_info),
        "n_ctx": props.get("n_ctx", settings.get("n_ctx")),
        "sampling": {
            key: settings[key]
            for key in ("temperature", "top_p", "top_k", "min_p", "presence_penalty")
            if key in settings
        },
        # Compaction changes what the model sees on every turn after the first
        # summary, so two runs under different compaction semantics are not the
        # same candidate and must not share a resume checkpoint.
        "compaction": _compaction_identity(config, keep_compaction),
        "opencode_version": opencode_version,
    }
    identity["fingerprint"] = session_eval.json_digest(identity)
    return identity


def _required_servers_preflight(env: Mapping[str, str]) -> None:
    completed = _bounded_run(
        [sys.executable, str(REPO_ROOT / "scripts" / "required_model_servers_check.py")],
        cwd=REPO_ROOT,
        env=env,
        timeout=60,
    )
    if completed.returncode != 0:
        raise session_eval.SessionEvalError("required model server preflight failed")


def _event_tool_error_count(output: str) -> int:
    count = 0
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        part = event.get("part")
        part = part if isinstance(part, Mapping) else {}
        event_type = str(event.get("type", "")).lower()
        part_type = str(part.get("type", "")).lower()
        if event_type not in ("tool_use", "tool-use") and part_type not in ("tool", "tool_use", "tool-use"):
            continue
        state = part.get("state", event.get("state"))
        if isinstance(state, Mapping) and state.get("status") == "error":
            count += 1
    return count


def _run_turn(
    *,
    root: Path,
    prompt: str,
    model: str,
    env: Mapping[str, str],
    timeout: int,
    session_id: str | None,
) -> tuple[dict[str, Any], str]:
    command = [
        "opencode",
        "run",
        "--dir",
        str(root),
        "--agent",
        "build",
        "--format",
        "json",
        "--title",
        "CodeTrail private session evaluation",
        "--model",
        model,
    ]
    if session_id:
        command.extend(("--session", session_id))
    command.append(prompt)
    started = time.monotonic()
    timed_out = False
    try:
        completed = _bounded_run(command, cwd=root, env=env, timeout=timeout)
        stdout_bytes = completed.stdout
        returncode = completed.returncode
    except _CommandTimedOut as exc:
        # OpenCode emits its JSON event stream incrementally.  A timeout is a
        # model outcome, not a reason to discard earlier cases or private
        # cleanup metadata.  Parse only the bounded partial stdout; stderr is
        # deliberately not persisted because it may contain project details.
        timed_out = True
        stdout_bytes = exc.stdout
        returncode = 124
    latency_ms = round((time.monotonic() - started) * 1000)
    try:
        stdout = stdout_bytes.decode("utf-8", errors="replace")
    except AttributeError:
        stdout = str(stdout_bytes)
    trace = parse_event_stream(
        stdout,
        latency_ms=latency_ms,
        harness_error=returncode != 0,
    )
    sessions = list(trace.session_ids)
    if session_id:
        if session_id not in sessions:
            sessions.append(session_id)
        resolved_session = session_id
    elif len(sessions) == 1:
        resolved_session = sessions[0]
    else:
        raise session_eval.SessionEvalError("OpenCode replay did not expose exactly one session id")
    calls = [
        {
            "tool": call.bare_tool,
            "identity_digest": session_eval.text_digest(call.identity),
        }
        for call in trace.completed_calls
    ]
    return (
        {
            "assistant_text": trace.assistant_text,
            "terminal": trace.terminal,
            "harness_error": trace.harness_error,
            "timed_out": timed_out,
            "calls": calls,
            "tool_error_count": _event_tool_error_count(stdout),
            "tokens": {
                "prompt": trace.prompt_tokens,
                "output": trace.output_tokens,
                "reasoning": trace.reasoning_tokens,
                "cache": trace.cache_tokens,
                "total": trace.total_tokens,
            },
            "latency_ms": trace.latency_ms,
            "compaction_events": trace.compaction_events,
        },
        resolved_session,
    )


def _delete_generated_session(session_id: str, *, root: Path, env: Mapping[str, str]) -> bool:
    if not _SAFE_SESSION_ID_RE.fullmatch(session_id):
        return False
    try:
        completed = _bounded_run(
            ["opencode", "session", "delete", session_id],
            cwd=root,
            env=env,
            timeout=30,
        )
    except session_eval.SessionEvalError:
        return False
    return completed.returncode == 0


def _run_case(
    case: Mapping[str, Any],
    *,
    model: str,
    env: Mapping[str, str],
    timeout: int,
    keep_sessions: bool,
) -> dict[str, Any]:
    root = _validate_project_root(case["project_root"])
    before = project_state_digest(root, case.get("state_paths", []))
    turn_results: list[dict[str, Any]] = []
    generated_session: str | None = None
    cleanup_ok = True
    try:
        for turn in case["turns"]:
            result, generated_session = _run_turn(
                root=root,
                prompt=turn["text"],
                model=model,
                env={**env, "AICODE_ROOT": str(root)},
                timeout=timeout,
                session_id=generated_session,
            )
            turn_results.append(result)
            if result["harness_error"] or not result["terminal"]:
                break
    finally:
        if generated_session and not keep_sessions:
            cleanup_ok = _delete_generated_session(generated_session, root=root, env=env)
    after = project_state_digest(root, case.get("state_paths", []))
    if before != after:
        raise session_eval.SessionEvalError(
            f"project state changed during read-only replay for case {case['id']}"
        )
    checks = session_eval.evaluate_checks(case["verifier"]["checks"], turn_results)
    automatic_pass = all(item["passed"] for item in checks) if checks else None
    if not cleanup_ok:
        automatic_pass = False
    return {
        "case_id": case["id"],
        "task_type": case["task_type"],
        "oracle_kind": case["verifier"]["oracle_kind"],
        "project_state_digest": before,
        "turns": turn_results,
        "automatic_checks": checks,
        "automatic_pass": automatic_pass,
        "cleanup_ok": cleanup_ok,
    }


def _candidate_result_payload(
    suite: Mapping[str, Any],
    candidate: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    auto_measured = [case for case in cases if case["automatic_pass"] is not None]
    return {
        "schema_version": session_eval.SCHEMA_VERSION,
        "suite_digest": session_eval.suite_digest(suite),
        "candidate": dict(candidate),
        "cases": [dict(case) for case in cases],
        "aggregate": {
            "complete": len(cases) == len(suite["cases"]),
            "case_count": len(cases),
            "suite_case_count": len(suite["cases"]),
            "automatic_measured": len(auto_measured),
            "automatic_passed": sum(
                case["automatic_pass"] is True for case in auto_measured
            ),
            "harness_failures": sum(
                any(turn["harness_error"] or not turn["terminal"] for turn in case["turns"])
                for case in cases
            ),
            "timeouts": sum(
                any(turn.get("timed_out") is True for turn in case["turns"])
                for case in cases
            ),
            "cleanup_failures": sum(case["cleanup_ok"] is not True for case in cases),
        },
    }


def _resume_cases(
    suite: Mapping[str, Any],
    candidate: Mapping[str, Any],
    saved_value: object,
) -> list[dict[str, Any]]:
    saved = session_eval.validate_candidate_result(saved_value)
    if saved["suite_digest"] != session_eval.suite_digest(suite):
        raise session_eval.SessionEvalError("resume checkpoint belongs to a different suite")
    if saved["candidate"] != candidate:
        raise session_eval.SessionEvalError(
            "resume checkpoint candidate/model fingerprint does not match the live server"
        )
    suite_cases = suite["cases"]
    saved_cases = saved["cases"]
    expected_prefix = [case["id"] for case in suite_cases[: len(saved_cases)]]
    if [case["case_id"] for case in saved_cases] != expected_prefix:
        raise session_eval.SessionEvalError(
            "resume checkpoint cases are not an exact ordered suite prefix"
        )
    is_complete = len(saved_cases) == len(suite_cases)
    if saved["aggregate"].get("complete") is not is_complete:
        raise session_eval.SessionEvalError("resume checkpoint completion marker is inconsistent")

    # A checkpoint is reusable only against the same live project state.  This
    # prevents a long overnight run from silently comparing different trees.
    for case, saved_case in zip(suite_cases, saved_cases, strict=True):
        root = _validate_project_root(case["project_root"])
        current = project_state_digest(root, case.get("state_paths", []))
        if saved_case["project_state_digest"] != current:
            raise session_eval.SessionEvalError(
                f"resume checkpoint project state drifted for case {case['id']}"
            )
    return [dict(case) for case in saved_cases]


def command_run(args: argparse.Namespace) -> int:
    suite = _load_suite(args.suite)
    if not _SAFE_CANDIDATE_LABEL_RE.fullmatch(args.candidate_label):
        raise session_eval.SessionEvalError(
            "--candidate-label must match [a-z][a-z0-9_]{2,79}"
        )
    output_dir = args.output_dir.expanduser()
    output_name = f"result-{args.candidate_label}.json"
    output_path = output_dir / output_name
    _config_path, global_config = _global_config()
    evaluation_config = _evaluation_config(
        global_config, args.model, keep_compaction=bool(getattr(args, "keep_compaction", False))
    )
    bare = args.model.split("/", 1)[1] if "/" in args.model else ""
    env = os.environ.copy()
    env.update(
        {
            "AICODE_MODEL": bare,
            "AI_CODE_COLLECT_DATA": "0",
            "AI_CODE_PATCH": "0",
            "AI_CODE_RUN_TESTS": "0",
            "AICODE_LESSONS_SKIP": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_EXPERIMENTAL_CODE_MODE": "false",
        }
    )
    with tempfile.TemporaryDirectory(prefix="codetrail-session-eval-") as raw_temp:
        temp_dir = Path(raw_temp)
        temp_dir.chmod(0o700)
        config_path = _write_temp_config(temp_dir, evaluation_config)
        env["OPENCODE_CONFIG"] = str(config_path)
        keep_compaction = bool(getattr(args, "keep_compaction", False))
        # 版本一律量:一般 replay 跑的是上游原生壓縮,1.18.16 與 1.18.21 的
        # 壓縮語意不同,前半後半混在一起就不可比。
        opencode_version = read_opencode_version(REPO_ROOT, env)
        env[COMPACTION_VERSION_ENV] = opencode_version
        if keep_compaction:
            # The plugin refuses any ownership state that is not bound to the
            # config actually in effect, and the replay config is a fresh temp
            # path.  Write a throw-away state bound to *that* path inside the
            # 0700 temp dir and point the plugin at it; every other check
            # (owner-only, no symlink, digest, mode) still applies.
            env[COMPACTION_STATE_ENV] = str(
                _write_replay_compaction_state(temp_dir, evaluation_config, config_path)
            )
            _require_compaction_runtime(evaluation_config, args.model, opencode_version)
        identity = _candidate_identity(
            model=args.model, config=evaluation_config, env=env,
            keep_compaction=keep_compaction, opencode_version=opencode_version,
        )
        candidate = {
            "label": args.candidate_label,
            "model": args.model,
            "fingerprint": identity["fingerprint"],
            "identity": identity,
        }
        if not args.skip_aux_preflight:
            _required_servers_preflight(env)
        cases: list[dict[str, Any]] = []
        checkpoint_exists = os.path.lexists(output_path)
        if checkpoint_exists and not args.resume:
            raise session_eval.SessionEvalError(
                f"candidate result already exists; use --resume or a new label: {output_path}"
            )
        if checkpoint_exists:
            cases = _resume_cases(
                suite,
                candidate,
                session_eval.read_json_file(output_path),
            )
            _print(f"validated checkpoint: {len(cases)}/{len(suite['cases'])} case(s)")
            if len(cases) == len(suite["cases"]):
                _print(f"candidate result already complete: {output_path}")
                return 0
        elif args.resume:
            _print("no checkpoint found; starting a new candidate run")

        for index, case in enumerate(suite["cases"], start=1):
            if index <= len(cases):
                continue
            _print(f"running {index}/{len(suite['cases'])}: {case['id']}")
            cases.append(
                _run_case(
                    case,
                    model=args.model,
                    env=env,
                    timeout=args.turn_timeout,
                    keep_sessions=args.keep_sessions,
                )
            )
            result = _candidate_result_payload(suite, candidate, cases)
            session_eval.validate_candidate_result(result)
            session_eval.write_private_json(output_dir, output_name, result)
            _print(f"checkpointed {len(cases)}/{len(suite['cases'])} case(s)")

    result = _candidate_result_payload(suite, candidate, cases)
    if result["aggregate"]["complete"] is not True:
        raise session_eval.SessionEvalError("candidate run ended without a complete case set")
    output = output_path
    _print(f"candidate result written: {output}")
    return 0


def command_bundle(args: argparse.Namespace) -> int:
    suite = _load_suite(args.suite)
    left = session_eval.read_json_file(args.left)
    right = session_eval.read_json_file(args.right)
    bundle, key = session_eval.build_blind_bundle(suite, left, right)
    review_path = session_eval.write_private_json(args.output_dir, "review.json", bundle)
    key_path = session_eval.write_private_json(args.output_dir, "review-key.json", key)
    markdown_path = session_eval.write_private_bytes(
        args.output_dir,
        "review.md",
        session_eval.render_review_markdown(bundle).encode("utf-8"),
    )
    _print(f"blind review JSON: {review_path}")
    _print(f"blind review Markdown: {markdown_path}")
    _print(f"sealed model mapping (do not open before review): {key_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mine private OpenCode sessions and compare local chat models"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="export explicit OpenCode sessions into private storage")
    export.add_argument("--session", action="append", required=True, help="OpenCode session id")
    export.add_argument("--output-dir", type=Path, default=DEFAULT_PRIVATE_DIR / "exports")
    export.add_argument("--sanitize", action="store_true", help="ask OpenCode to redact transcript/file data")
    export.set_defaults(func=command_export)

    mine = sub.add_parser("mine", help="remove assistant turns and produce user-only draft timelines")
    mine.add_argument("--source-dir", type=Path, required=True)
    mine.add_argument("--glob", default="*.json", help="export filename glob inside source-dir")
    mine.add_argument("--output-dir", type=Path, default=DEFAULT_PRIVATE_DIR)
    mine.add_argument("--output-name", default="drafts.json")
    mine.set_defaults(func=command_mine)

    validate = sub.add_parser("validate", help="validate a curated evidence-first suite")
    validate.add_argument("--suite", type=Path, required=True)
    validate.set_defaults(func=command_validate)

    run = sub.add_parser("run", help="replay one curated suite against the currently loaded model")
    run.add_argument("--suite", type=Path, required=True)
    run.add_argument("--candidate-label", required=True, help="private stable label used only in sealed results")
    run.add_argument("--model", required=True, help="OpenCode provider/model id")
    run.add_argument("--output-dir", type=Path, default=DEFAULT_PRIVATE_DIR / "runs")
    run.add_argument("--turn-timeout", type=int, default=DEFAULT_TURN_TIMEOUT)
    run.add_argument("--skip-aux-preflight", action="store_true", help="only for suites that cannot call RAG/VL")
    run.add_argument("--keep-sessions", action="store_true", help="retain generated eval sessions for debugging")
    run.add_argument(
        "--keep-compaction",
        action="store_true",
        help=(
            "keep BOTH halves of the compaction contract in the replay: the "
            "managed compaction.* settings and the CodeTrail compaction plugin. "
            "Only for a suite whose subject is compaction itself; every other "
            "suite runs with both removed"
        ),
    )
    run.add_argument(
        "--resume",
        action="store_true",
        help="validate and continue an atomic per-case checkpoint for this label",
    )
    run.set_defaults(func=command_run)

    bundle = sub.add_parser("bundle", help="randomize two result files into an anonymous A/B review")
    bundle.add_argument("--suite", type=Path, required=True)
    bundle.add_argument("--left", type=Path, required=True)
    bundle.add_argument("--right", type=Path, required=True)
    bundle.add_argument("--output-dir", type=Path, default=DEFAULT_PRIVATE_DIR / "review")
    bundle.set_defaults(func=command_bundle)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "turn_timeout", DEFAULT_TURN_TIMEOUT) not in range(1, 3601):
        _print("--turn-timeout must be in 1..3600", error=True)
        return 2
    try:
        return int(args.func(args))
    except session_eval.SessionEvalError as exc:
        _print(f"FAIL — {exc}", error=True)
        return 2
    except (deployment_profile.ProfileError, ValueError) as exc:
        _print(f"FAIL — {type(exc).__name__}", error=True)
        return 2
    except (EvalError, compaction_mode.CompactionModeError) as exc:
        # `read_opencode_version` 失敗會丟 eval_tool_routing 的 EvalError,
        # 壓縮狀態寫不出來會丟 CompactionModeError。兩者都在 commit 之前安全
        # 失敗,但沒有接的話會吐 traceback 而不是既有的乾淨診斷。
        _print(f"FAIL — {exc}", error=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
