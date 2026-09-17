#!/usr/bin/env python3
"""Mine private CodeTrail sessions and run evidence-first model comparisons.

This is a manual, explicitly authorised eval lane.  It is not part of pytest,
CI, ``aicode`` startup, or the checked-in NDA-safe fixtures under ``eval/``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import client_compaction  # noqa: E402
import process_env  # noqa: E402
import client_config  # noqa: E402
import compaction_formula  # noqa: E402
import deployment_profile
import model_resolution  # noqa: E402
from model_identity import ModelIdentityError  # noqa: E402
import root_safety  # noqa: E402
import client_mcp  # noqa: E402
import session_eval  # noqa: E402
from scripts.eval_tool_routing import (  # noqa: E402
    EvalError,
    LocalJsonClient,
    parse_event_stream,
)

DEFAULT_PRIVATE_DIR = REPO_ROOT / ".codetrail" / "session_eval"
MAX_SUBPROCESS_OUTPUT_BYTES = 64 * 1024 * 1024
DEFAULT_TURN_TIMEOUT = 600
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_SAFE_CANDIDATE_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]{2,79}$")

# Defense in depth for replay against a real private project.  The client's
# read-only policy denies every tool the server does not annotate readOnlyHint,
# while the MCP server is separately started with --readonly.
# 這份名單是**測試釘住的下限**,不是判準:判準是 readOnlyHint,所以漏加名單的
# 新工具一樣被 deny。名字改成裸名(客戶端沒有 `codetrail_` 前綴)。
MUTATING_FRONTEND_TOOLS = (
    "apply_patch",
    "run_lint",
    "run_command",
    "ingest_document",
    "remove_document",
    "review_figures",
    "import_external_file",
    "record_lesson",
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
    timeout: int,
) -> process_env.CompletedProcess[bytes]:
    try:
        completed = process_env.run(
            list(command),
            cwd=str(cwd),
            stdin=process_env.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise session_eval.SessionEvalError(f"command is unavailable: {command[0]}") from exc
    except process_env.TimeoutExpired as exc:
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
    """從 CodeTrail 自家 session store 匯出一個對話。"""
    import client_store

    # export 的 root 是**目前目錄**:session store 綁 root 雜湊,而使用者是
    # `cd <專案>` 之後跑這支的。以前這裡認 `AICODE_ROOT`,殼層裡殘留一個別的
    # 專案的值就會去別的 store 找,然後回報「沒有這個 session」。
    root = os.getcwd()
    store = client_store.SessionStore(root)
    try:
        records = store.read(session_id)
    except client_store.SessionStoreError as exc:
        raise session_eval.SessionEvalError(f"session 讀取失敗: {exc}") from exc
    exported = session_eval.validate_session_export(
        session_eval.export_from_store(session_id, records, sanitized=sanitized)
    )
    name = f"{session_eval.session_hash(session_id)}.json"
    return session_eval.write_private_json(output_dir, name, exported)


def command_export(args: argparse.Namespace) -> int:
    sessions = list(dict.fromkeys(args.session or []))
    if not sessions:
        raise session_eval.SessionEvalError("export needs at least one --session")
    if len(sessions) > session_eval.MAX_SESSIONS:
        raise session_eval.SessionEvalError("too many sessions requested")
    output_dir = _private_output_dir(args.output_dir)
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
    output = session_eval.write_private_json(
        _private_output_dir(args.output_dir), args.output_name, corpus
    )
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


def _private_output_dir(raw: Path) -> Path:
    """私人產物(prompt、candidate answer、盲測 key)只准落在兩種地方:

    repo 之外,或 repo 裡 git ignore 的 ``.codetrail/``。指到 ``<repo>/eval/`` 這種
    可追蹤的位置會把 NDA 內容送進下一次 commit。
    """
    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    real = Path(os.path.realpath(target))
    repo = Path(os.path.realpath(REPO_ROOT))
    if real == repo or repo in real.parents:
        private = repo / ".codetrail"
        if real != private and private not in real.parents:
            raise session_eval.SessionEvalError(
                f"output directory {target} is inside the repository but not under "
                f"{private}; private eval artifacts must not land in a tracked path"
            )
    return target


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
    completed = _bounded_run(command, cwd=root, timeout=120)
    digest.update(command[1].encode("utf-8") if len(command) > 1 else b"")
    digest.update(str(completed.returncode).encode("ascii"))
    digest.update(completed.stdout)


def project_state_digest(root: Path, state_paths: Sequence[str]) -> str:
    """Hash reproducibility state without serialising paths or file contents."""

    digest = hashlib.sha256()
    git_probe = _bounded_run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
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
        if target.is_symlink():
            raise session_eval.SessionEvalError("suite state paths must not be symlinks")
        if stat.S_ISDIR(st.st_mode):
            # `.codetrail` 是目錄:逐檔雜湊(排序過),名字與內容都算數。
            # 只 hash 目錄本身的 mtime 會漏掉「改了一個檔但大小相同」。
            digest.update(b"dir")
            for child in sorted(target.rglob("*")):
                try:
                    child_st = child.stat(follow_symlinks=False)
                except OSError:
                    continue
                digest.update(str(child.relative_to(target)).encode("utf-8"))
                if not stat.S_ISREG(child_st.st_mode):
                    digest.update(b"non-regular")
                    continue
                digest.update(str(child_st.st_size).encode("ascii"))
                try:
                    with child.open("rb") as handle:
                        for block in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(block)
                except OSError:
                    digest.update(b"unreadable")
            continue
        if not stat.S_ISREG(st.st_mode):
            raise session_eval.SessionEvalError("suite state paths must be regular files")
        digest.update(str(st.st_size).encode("ascii"))
        try:
            with target.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise session_eval.SessionEvalError("cannot hash a suite state path") from exc
    return digest.hexdigest()










_FILE_PROMPT_REFERENCE_RE = re.compile(r"^\{file:(.+)\}$", re.DOTALL)




def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unavailable"






def replay_client_config(*, keep_compaction: bool) -> dict[str, Any]:
    """Replay settings freeze behavior and inherit only trusted transport authorization.

    frozen suite 的可比性要求每個 candidate 在同一組壓縮語意下跑。讀使用者的
    設定等於「同一份 suite 在兩台機器上量到不同東西」;而一台從來沒設過
    client.json 的新部署會直接沒有壓縮,卻沒有任何欄位記得這件事。

    * 預設 ``off``:一般 replay 量的是模型本身,不是壓縮。
    * ``--keep-compaction``:唯一以壓縮為主題的那個 suite,用 ``codetrail``。
    """
    value = {
        "schema": client_config.SCHEMA,
        "compaction_mode": (
            client_compaction.MODE_CODETRAIL if keep_compaction else client_compaction.MODE_OFF
        ),
        # replay 是唯讀的:寫入工具在 policy 與 MCP server 兩層都已經關掉,
        # 這裡不再開任何覆寫。
        "permission": {},
        # 專案內的 AGENTS.md / lessons 不進 replay 的 system prompt:
        # frozen suite 的可比性要求每個 candidate 看到同一份指示。
        "project_instructions": False,
    }
    # Only transport authorization is inherited from the trusted owner-only
    # settings. Evaluation semantics/permissions remain frozen above.
    settings = client_config.load_client_settings()
    if settings.model_endpoints:
        value["model_endpoints"] = dict(settings.model_endpoints)
    if settings.model_remote_ok:
        value["model_remote_ok"] = True
    if settings.kb_context_remote_ok:
        value["kb_context_remote_ok"] = True
    return value


def _write_replay_client_config(directory: Path, value: Mapping[str, Any]) -> Path:
    from runtime_dependencies import require_safe_filesystem

    require_safe_filesystem("private replay config", owner_only=True,
                            error_type=session_eval.SessionEvalError)
    path = directory / "client.json"
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


def _compaction_identity(*, keep_compaction: bool, n_ctx: Any) -> dict[str, Any]:
    """這次 replay 跑在什麼壓縮語意底下。

    兩個在這裡不同的 run 不可比,也不得共用 resume checkpoint:一個會在 idle
    依 CodeTrail 的規則壓縮,另一個完全不壓,而兩半結果會被靜默合併。
    """
    identity: dict[str, Any] = {
        "keep_compaction": bool(keep_compaction),
        "mode": (
            client_compaction.MODE_CODETRAIL if keep_compaction else client_compaction.MODE_OFF
        ),
        "rules_digest": _file_digest(compaction_formula.RULES_DOC),
    }
    if not keep_compaction:
        return identity
    # 壓縮真的要能發生:推不出門檻的 n_ctx 會讓「有壓縮」這件事靜默消失,
    # 而 `compaction_events` 照樣讀到 0。
    if not isinstance(n_ctx, int) or isinstance(n_ctx, bool):
        raise session_eval.SessionEvalError(
            "--keep-compaction needs the live n_ctx from /props to derive a threshold"
        )
    try:
        derived = client_compaction.derive(n_ctx)
    except compaction_formula.CompactionModeError as exc:
        raise session_eval.SessionEvalError(
            f"--keep-compaction cannot derive a compaction threshold for n_ctx={n_ctx}: {exc}"
        ) from exc
    identity["threshold"] = derived.idle_threshold
    identity["preserve_recent_tokens"] = derived.preserve_recent_tokens
    return identity


def client_identity() -> str:
    """跑這次 replay 的客戶端身分。"""
    parts = []
    for name in ("client_engine.py", "client_prompt.py", "codetrail_chat.py"):
        try:
            parts.append(
                session_eval.text_digest((REPO_ROOT / name).read_text(encoding="utf-8"))
            )
        except OSError:
            parts.append("missing")
    return session_eval.text_digest("|".join(parts))


def _profile_env() -> dict[str, str]:
    """交給 `deployment_profile` 的環境:**只有 HOME**(Windows 的 USERPROFILE)。"""
    home = os.environ.get("HOME")
    if home:
        return {"HOME": home}
    profile = os.environ.get("USERPROFILE")
    return {"USERPROFILE": profile} if profile else {}


def _normalise_base_url(base_url: str | None = None) -> str:
    """candidate 模型的端點:deployment profile 是唯一來源。

    參數只保留給呼叫端**明確指定**的情況(測試)。以前這裡吃一個 env dict 再從
    裡面讀端點,而 production 傳進來的正是一份未剝除的行程環境 —— 等於殼層殘留
    一個值就能把 fingerprint 綁到別台機器的 server,而 suite 的可比性靠的就是
    那個 fingerprint。
    """
    import config as _config

    base = base_url or _config.LLAMA_BASE_URL
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


def bare_model(value: str) -> str:
    """`--model` 接受的三種寫法都化成客戶端真正用的那一個:

    - registry bare name(`qwen3-coder-30b`)原樣;
    - GGUF 路徑(絕對 / `~` / `.gguf` 結尾)原樣——`split("/")` 會把絕對路徑砍成相對的;
    - 舊式 `llamacpp/<bare>`(舊世代前端的 provider/model 寫法)剝掉前綴。
    外部 provider(openai/ 等)直接拒絕:CodeTrail 只跑本地 llama-server。
    """
    resolved = model_resolution.normalize_main_model(str(value or ""), "--model")
    if not resolved.ok:
        raise session_eval.SessionEvalError(resolved.error or f"invalid --model {value!r}")
    return resolved.model


def _candidate_identity(
    *,
    model: str,
    env: Mapping[str, str],
    keep_compaction: bool = False,
) -> dict[str, Any]:
    bare = bare_model(model)
    # registry 查表只交 HOME:`env` 是要遞給子行程的那一份(已剝掉 CodeTrail
    # 的設定名),而查表要的只是「models.json 在哪」。
    profile = deployment_profile.load_effective_profile(_profile_env())
    expected_path = None
    if profile.mode == "client":
        if bare != profile.service("main").model:
            raise session_eval.SessionEvalError("selected candidate differs from the client deployment model ID")
    else:
        expected_path = Path(deployment_profile.resolve_model_reference(bare, _profile_env(), must_exist=True))
    client = LocalJsonClient(_normalise_base_url(), timeout_seconds=120)
    props = client.get_json("/props")
    if expected_path is not None and not _same_model_artifact(expected_path, _path_from_props(props)):
        raise session_eval.SessionEvalError(
            "loaded llama-server artifact does not match the selected candidate model"
        )
    settings = props.get("default_generation_settings")
    settings = settings if isinstance(settings, Mapping) else {}
    build_info = props.get("build_info")
    from model_identity import capture_model_identity, capture_model_identities, artifact_digest
    runtime_identity = (capture_model_identity("main", profile=profile, props=props)
                        if profile.mode == "client" else None)
    auxiliary_identities = (capture_model_identities(("embedding", "reranker", "vl"), profile=profile)
                           if profile.mode == "client" else {})
    identity = {
        "selected_model": model,
        "artifact_digest": artifact_digest(expected_path) if expected_path is not None else None,
        "runtime_identity": runtime_identity,
        "auxiliary_identities": auxiliary_identities,
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
        "compaction": _compaction_identity(
            keep_compaction=keep_compaction,
            n_ctx=props.get("n_ctx", settings.get("n_ctx")),
        ),
        "client_version": client_identity(),
    }
    identity["fingerprint"] = session_eval.json_digest(identity)
    return identity


def _required_servers_preflight(env: Mapping[str, str]) -> None:
    completed = _bounded_run(
        [sys.executable, str(REPO_ROOT / "scripts" / "required_model_servers_check.py")],
        cwd=REPO_ROOT,
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
    client_config_path: Path,
    persist: bool = False,
    skip_aux_preflight: bool = False,
) -> tuple[dict[str, Any], str]:
    # read-only replay:客戶端這一層 deny 全部非唯讀工具,MCP server 那一層
    # 再以 `--readonly` 關一次(寫入、執行、build 命令、context metrics、資料收集)。
    #
    # 多輪 case 必須 `--persist --session`:每一輪各起一個 ephemeral 行程的話,
    # 模型完全看不到上一輪 —— 「第二輪要引用第一輪的結論」這種 case 量到的是
    # 一個不存在的能力。單輪 case 仍然不落檔(session 檔逐字含 NDA 內容)。
    command = [
        sys.executable,
        str(REPO_ROOT / "codetrail_chat.py"),
        "run",
        "--root",
        str(root),
        "--policy",
        "readonly",
        "--model",
        bare_model(model),
        "--client-config",
        str(client_config_path),
    ]
    if persist:
        command.append("--persist")
        if session_id:
            command.extend(["--session", session_id])
    if skip_aux_preflight:
        # 外層跳過附屬 server 硬閘時,每個 replay child 的 MCP 也要跳過;
        # 不然外層跳了、child 照樣在 preflight 失敗。
        command.append("--skip-aux-preflight")
    command.extend(["--format", "json", prompt])
    started = time.monotonic()
    timed_out = False
    try:
        completed = _bounded_run(command, cwd=root, timeout=timeout)
        stdout_bytes = completed.stdout
        returncode = completed.returncode
    except _CommandTimedOut as exc:
        # The client emits its JSON event stream incrementally.  A timeout is a
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
        raise session_eval.SessionEvalError("replay did not expose exactly one session id")
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


#: replay 一定會看的專案內狀態。`.gitignore` 掉的東西不會出現在 git diff 裡,
#: 但它們正是唯讀 replay 最可能被寫到的地方(KB、context metrics、上傳暫存)。
#: suite 沒列 state_paths 時仍然要驗這幾條,否則「前後 project state 不變」
#: 這個保證可以被三個明訂路徑繞過。
DEFAULT_STATE_PATHS = (
    ".codetrail",
    "knowledge.json",
    ".aicode_uploads",
)


def _state_paths(case: Mapping[str, Any]) -> list[str]:
    listed = case.get("state_paths") or []
    return sorted({*(str(item) for item in listed), *DEFAULT_STATE_PATHS})


def _delete_generated_session(session_id: str, *, root: Path, env: Mapping[str, str]) -> bool:
    """多輪 replay 會落檔(模型要看得到上一輪);跑完就刪掉。

    session 檔逐字含 NDA prompt 與工具輸出,不能留在使用者的 session 清單裡。
    """
    try:
        import client_store

        # store 的位置由 HOME / XDG_STATE_HOME 推導;把 replay 的 env **傳進去**,
        # 不是暫時換掉整個 os.environ 再還原(那條路曾是「複製整份環境」的形狀,
        # 而且例外時會把別的執行緒看到的環境一起換掉)。
        client_store.SessionStore(root, env=env).delete(session_id)
    except Exception:  # noqa: BLE001 - 刪不掉要回報,不是丟 traceback
        return False
    return True


def _run_case(
    case: Mapping[str, Any],
    *,
    model: str,
    env: Mapping[str, str],
    timeout: int,
    keep_sessions: bool,
    client_config_path: Path,
    skip_aux_preflight: bool = False,
) -> dict[str, Any]:
    root = _validate_project_root(case["project_root"])
    state_paths = _state_paths(case)
    before = project_state_digest(root, state_paths)
    turn_results: list[dict[str, Any]] = []
    generated_session: str | None = None
    cleanup_ok = True
    # 只有多輪 case 需要落檔(模型要看得到上一輪)。單輪維持 ephemeral。
    persist = len(case["turns"]) > 1
    failure: BaseException | None = None
    try:
        try:
            for turn in case["turns"]:
                result, generated_session = _run_turn(
                    root=root,
                    prompt=turn["text"],
                    model=model,
                    env=env,
                    timeout=timeout,
                    session_id=generated_session,
                    client_config_path=client_config_path,
                    persist=persist,
                    skip_aux_preflight=skip_aux_preflight,
                )
                turn_results.append(result)
                if result["harness_error"] or not result["terminal"]:
                    break
        finally:
            if persist and generated_session and not keep_sessions:
                cleanup_ok = _delete_generated_session(generated_session, root=root, env=env)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        # 最後一道防線**一定**要跑:replay child 改了現場之後丟例外,如果 digest
        # 放在 try 外面,那個改動就沒有人看到。
        after = project_state_digest(root, state_paths)
        if before != after:
            suffix = f"(after {type(failure).__name__})" if failure is not None else ""
            raise session_eval.SessionEvalError(
                f"project state changed during read-only replay for case {case['id']}{suffix}"
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
        current = project_state_digest(root, _state_paths(case))
        if saved_case["project_state_digest"] != current:
            raise session_eval.SessionEvalError(
                f"resume checkpoint project state drifted for case {case['id']}"
            )
    return [dict(case) for case in saved_cases]


def command_run(args: argparse.Namespace) -> int:
    client_config.apply_to_config(client_config.load_client_settings(), readonly=True)
    suite = _load_suite(args.suite)
    if not _SAFE_CANDIDATE_LABEL_RE.fullmatch(args.candidate_label):
        raise session_eval.SessionEvalError(
            "--candidate-label must match [a-z][a-z0-9_]{2,79}"
        )
    output_dir = _private_output_dir(args.output_dir)
    output_name = f"result-{args.candidate_label}.json"
    output_path = output_dir / output_name
    keep_compaction = bool(getattr(args, "keep_compaction", False))
    bare = bare_model(args.model)
    # replay 的每一項設定都走 **argv** 或 replay 自己那份 client.json:
    #   * 模型 → `run --model`
    #   * 寫入 / 執行 / context metrics / 資料收集 → `run --policy readonly`
    #     (它同時讓 MCP 以 `--readonly` 起、讓客戶端 `apply_to_config(readonly=True)`)
    #   * 專案內 AGENTS.md 與 lessons → replay client.json 的 `project_instructions=false`
    # 環境不再帶任何 CodeTrail 設定;子行程的環境在交出去之前也會被剝乾淨。
    env = client_mcp.child_env()
    with tempfile.TemporaryDirectory(prefix="codetrail-session-eval-") as raw_temp:
        temp_dir = Path(raw_temp)
        temp_dir.chmod(0o700)
        # replay 用自己寫的 client.json,只繼承使用者的端點授權:同一份 suite 在兩台
        # 機器上必須量到同一件事,而一台沒設過 client.json 的新部署會直接沒有
        # 壓縮(而且沒有任何欄位記得)。
        client_config_path = _write_replay_client_config(
            temp_dir, replay_client_config(keep_compaction=keep_compaction)
        )
        identity = _candidate_identity(
            model=args.model, env=env, keep_compaction=keep_compaction,
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
                    client_config_path=client_config_path,
                    skip_aux_preflight=bool(args.skip_aux_preflight),
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
    bundle_dir = _private_output_dir(args.output_dir)
    review_path = session_eval.write_private_json(bundle_dir, "review.json", bundle)
    key_path = session_eval.write_private_json(bundle_dir, "review-key.json", key)
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
        description="Mine private CodeTrail sessions and compare local chat models"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="export explicit CodeTrail sessions into private storage")
    export.add_argument("--session", action="append", required=True, help="CodeTrail session id")
    export.add_argument("--output-dir", type=Path, default=DEFAULT_PRIVATE_DIR / "exports")
    export.add_argument("--sanitize", action="store_true", help="strip assistant text and tool output (keep tool names) for sharing")
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
    run.add_argument("--model", required=True, help="model for headless replay: registry name or GGUF path (legacy llamacpp/<name> is accepted and stripped); the value is kept verbatim in the result identity")
    run.add_argument("--output-dir", type=Path, default=DEFAULT_PRIVATE_DIR / "runs")
    run.add_argument("--turn-timeout", type=int, default=DEFAULT_TURN_TIMEOUT)
    run.add_argument("--skip-aux-preflight", action="store_true", help="only for suites that cannot call RAG/VL")
    run.add_argument("--keep-sessions", action="store_true", help="retain generated eval sessions for debugging")
    run.add_argument(
        "--keep-compaction",
        action="store_true",
        help=(
            "replay with the client's own structured compaction (mode codetrail) "
            "instead of compaction off. Only for a suite whose subject is "
            "compaction itself; every other suite runs with compaction off"
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
    except (EvalError, compaction_formula.CompactionModeError, ModelIdentityError,
            client_config.ClientConfigError) as exc:
        # 壓縮門檻推不出來會丟 CompactionModeError,catalog 契約會丟 EvalError。
        # 兩者都在 commit 之前安全失敗,但沒有接的話會吐 traceback 而不是既有
        # 的乾淨診斷。
        _print(f"FAIL — {exc}", error=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
