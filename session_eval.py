#!/usr/bin/env python3
"""Private, evidence-first evaluation helpers for CodeTrail sessions.

The historical assistant response is useful for finding failure modes, but it
is never an oracle.  This module therefore has two deliberately separate data
shapes:

* a mined draft contains only user-authored text and weak interaction signals;
* a curated suite contains user/evidence turns plus verifier contracts, with no
  reference-answer field at all.

Raw exports, curated NDA prompts and candidate answers are private artifacts.
Writers in this module keep them below one explicitly chosen directory, reject
symlinks, and create directories/files as 0700/0600.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
MAX_EXPORT_BYTES = 64 * 1024 * 1024
MAX_SESSIONS = 500
MAX_MESSAGES_PER_SESSION = 10_000
MAX_USER_TEXT_CHARS = 2_000_000
MAX_CASES = 200
MAX_TURNS_PER_CASE = 32
MAX_TURN_CHARS = 100_000

TASK_TYPES = frozenset(
    {
        "code_qa",
        "firmware_debug",
        "code_change",
        "rag_spec",
        "tool_use",
        "writing",
        "planning",
        "other",
    }
)
ORACLE_KINDS = frozenset(
    {"deterministic", "external_outcome", "human_pairwise", "unscored"}
)
TURN_KINDS = frozenset({"prompt", "evidence", "constraint"})
CHECK_TYPES = frozenset(
    {
        "terminal",
        "required_tool",
        "required_any_tool",
        "forbidden_tool",
        "required_text_all",
        "required_text_any",
        "forbidden_text",
        "max_identical_tool_call",
        "no_tool_error",
    }
)

# A suite containing one of these keys has silently reintroduced a model answer
# as truth.  Match recursively and case-insensitively so nesting cannot bypass
# the contract.
FORBIDDEN_ORACLE_KEYS = frozenset(
    {
        "expected_answer",
        "reference_answer",
        "gold_answer",
        "assistant_answer",
        "model_answer",
    }
)

_SAFE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,79}$")
_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")

# Short rebukes are useful negative labels, but make no sense as scripted
# counterfactual user turns once the old assistant answer is removed.
_INTERACTION_ONLY_RE = re.compile(
    r"^(?:好|好的|嗯|用啊|你用啊|不要只回答|使用工具|使用codetrail工具|"
    r"又在胡說八道|不要跳針|\.\.|再試一次)[。！!？?\s]*$",
    re.IGNORECASE,
)
_CORRECTION_PREFIX_RE = re.compile(
    r"^(?:你(?:這|又|還是)?(?:寫|說|算|看)?錯(?:了|的)?(?:吧)?|"
    r"你是不是(?:沒|又)|不是這樣|不對|可是你|但你)(?:[，,：:\s-]+)?",
    re.IGNORECASE,
)
_DEPENDENT_RE = re.compile(
    r"(?:前面|上一(?:則|個)|你剛(?:才)?|照你這樣|那你|這個|這樣|後者|前者|"
    r"回到這點|再(?:次)?看(?:看)?|還是)",
    re.IGNORECASE,
)


class SessionEvalError(RuntimeError):
    """A private corpus, suite, replay or review artifact is invalid."""


@dataclass(frozen=True)
class PrivateDirectory:
    """An opened owner-only output directory anchored by a directory fd."""

    path: Path
    fd: int

    def close(self) -> None:
        os.close(self.fd)

    def __enter__(self) -> PrivateDirectory:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def session_hash(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise SessionEvalError("session export has an invalid session id")
    return text_digest(session_id)[:16]


_REPO_ROOT = Path(__file__).resolve().parent


def _private_error(message: str) -> SessionEvalError:
    return SessionEvalError(message)


def _private_directory(path: Path) -> PrivateDirectory:
    """Create/open one private directory through the shared owner-only guard.

    repo 底下的私人目錄(``.codetrail/session_eval/...``)以 repo root 當 anchor:
    ``.codetrail`` 以下逐層 ``O_NOFOLLOW``;repo 以外的自訂輸出目錄以它的父目錄
    當 anchor(祖先 realpath 解析,最後一層不跟 symlink)。
    """
    import client_paths

    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    anchor = _REPO_ROOT if _REPO_ROOT in target.parents else target.parent

    def _guard(resolved: Path) -> None:
        # 私人產物只准落在 repo 之外或 repo 內 git ignore 的 `.codetrail/`;
        # 每一次開啟都判(anchor 以上的 symlink 可以在兩次操作之間被改指)。
        repo = Path(os.path.realpath(_REPO_ROOT))
        private = repo / ".codetrail"
        if (resolved == repo or repo in resolved.parents) and not (
            resolved == private or private in resolved.parents
        ):
            raise SessionEvalError(
                f"private output directory {resolved} is inside the repository "
                f"but not under {private}"
            )

    fd = client_paths.open_private_dir(target, _private_error, anchor=anchor, guard=_guard)
    return PrivateDirectory(target, fd)


def _safe_flat_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise SessionEvalError("private artifact name must be one safe path component")
    return name


def write_private_bytes(directory: Path, name: str, payload: bytes) -> Path:
    """Atomically write one flat 0600 file below an anchored 0700 directory."""

    safe_name = _safe_flat_name(name)
    if not isinstance(payload, bytes):
        raise SessionEvalError("private artifact payload must be bytes")
    with _private_directory(directory) as opened:
        try:
            existing = os.stat(safe_name, dir_fd=opened.fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise SessionEvalError(f"cannot inspect private artifact {safe_name}") from exc
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise SessionEvalError(f"refusing non-regular private artifact: {safe_name}")

        tmp_name = f".{safe_name}.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = -1
        try:
            fd = os.open(tmp_name, flags, 0o600, dir_fd=opened.fd)
            with os.fdopen(fd, "wb", closefd=True) as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                tmp_name,
                safe_name,
                src_dir_fd=opened.fd,
                dst_dir_fd=opened.fd,
            )
            os.chmod(safe_name, 0o600, dir_fd=opened.fd, follow_symlinks=False)
            os.fsync(opened.fd)
        except OSError as exc:
            raise SessionEvalError(f"cannot write private artifact {safe_name}") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_name, dir_fd=opened.fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
    return Path(directory).expanduser() / safe_name


def write_private_json(directory: Path, name: str, value: object) -> Path:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    return write_private_bytes(directory, name, data)


def read_json_file(path: Path, *, max_bytes: int = MAX_EXPORT_BYTES) -> object:
    target = Path(path).expanduser()
    try:
        if target.is_symlink() or not target.is_file():
            raise SessionEvalError(f"JSON input must be a regular non-symlink file: {target}")
        size = target.stat(follow_symlinks=False).st_size
        if size > max_bytes:
            raise SessionEvalError(f"JSON input exceeds {max_bytes} bytes: {target}")
        raw = target.read_bytes()
    except SessionEvalError:
        raise
    except OSError as exc:
        raise SessionEvalError(f"cannot read JSON input: {target}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionEvalError(f"invalid UTF-8 JSON input: {target}") from exc


def _message_role(message: Mapping[str, Any]) -> str:
    info = message.get("info")
    role = info.get("role") if isinstance(info, Mapping) else None
    return role if isinstance(role, str) else ""


def _user_text(message: Mapping[str, Any]) -> str:
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, Mapping) or part.get("type") != "text":
            continue
        if part.get("synthetic") is True or part.get("ignored") is True:
            continue
        value = part.get("text")
        if isinstance(value, str) and value.strip():
            chunks.append(value.strip())
    return "\n".join(chunks).strip()


def validate_session_export(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SessionEvalError("session export root must be an object")
    info = value.get("info")
    messages = value.get("messages")
    if not isinstance(info, dict) or not isinstance(messages, list):
        raise SessionEvalError("session export must contain info object and messages array")
    session_id = info.get("id")
    session_hash(str(session_id) if isinstance(session_id, str) else "")
    if len(messages) > MAX_MESSAGES_PER_SESSION:
        raise SessionEvalError("session export has too many messages")
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise SessionEvalError(f"messages[{index}] must be an object")
        role = _message_role(message)
        if role not in ("user", "assistant"):
            raise SessionEvalError(f"messages[{index}] has unsupported role")
        if not isinstance(message.get("parts"), list):
            raise SessionEvalError(f"messages[{index}].parts must be an array")
    return value


def export_from_store(
    session_id: str,
    records: Sequence[Mapping[str, Any]],
    *,
    sanitized: bool = False,
) -> dict[str, Any]:
    """把 CodeTrail 自家 session store 的 JSONL 轉成 export 形狀。

    **raw export 是來源封存**:助理回答與工具
    結果都保留(它們是之後人工建 verifier 用的 file/tool evidence),寫進 0600 的
    私人目錄。``sanitized=True`` 才把助理文字與工具輸出拿掉(只留工具名),給要
    分享出去的那一份。

    「歷史助理回答不得當 oracle」是**挖掘層**(``draft_from_export`` 只讀 user
    text)與 suite schema 的契約,不是靠匯出時把證據丟掉來達成。
    """
    messages: list[dict[str, Any]] = []
    for record in records:
        if record.get("type") == "compaction":
            # append-only 檔裡原始對話都還在;compaction 記錄只是「送模型的那一份」
            # 從哪裡起算。來源封存要的是原始對話,所以這筆略過、**不**清掉前段。
            continue
        if record.get("type") != "message":
            continue
        role = record.get("role")
        content = record.get("content")
        if role == "user":
            if record.get("synthetic"):
                continue
            # 角色放在 `info.role`:validator 讀的是那一格(`_message_role`),
            # 頂層 `role` 會讓每一份匯出在第一則就被判成 unsupported role。
            messages.append(
                {
                    "info": {"role": "user"},
                    "parts": [{"type": "text", "text": content if isinstance(content, str) else ""}],
                }
            )
        elif role == "assistant":
            parts: list[dict[str, Any]] = []
            if isinstance(content, str) and content.strip() and not sanitized:
                parts.append({"type": "text", "text": content})
            for call in record.get("tool_calls") or ():
                function = call.get("function") if isinstance(call, Mapping) else None
                name = function.get("name") if isinstance(function, Mapping) else None
                part: dict[str, Any] = {"type": "tool", "tool": name or "?", "state": {"status": "called"}}
                if isinstance(call, Mapping) and isinstance(call.get("id"), str):
                    part["id"] = call["id"]
                arguments = function.get("arguments") if isinstance(function, Mapping) else None
                if not sanitized and isinstance(arguments, str):
                    # 參數是 verifier 要用的證據(哪個檔、哪個 pattern);分享版拿掉。
                    part["arguments"] = arguments
                parts.append(part)
            messages.append({"info": {"role": "assistant"}, "parts": parts})
        elif role == "tool":
            # 工具結果掛在宣告它的那則 assistant 底下(export 的形狀)。
            state: dict[str, Any] = {"status": record.get("tool_status") or "completed"}
            if not sanitized and isinstance(content, str):
                state["output"] = content
            part = {"type": "tool", "tool": record.get("name") or "?", "state": state}
            if isinstance(record.get("tool_call_id"), str):
                part["id"] = record["tool_call_id"]
            if messages and messages[-1]["info"]["role"] == "assistant":
                messages[-1]["parts"].append(part)
            else:
                messages.append({"info": {"role": "assistant"}, "parts": [part]})
    return {"info": {"id": session_id}, "messages": messages}


def neutralize_followup(text: str) -> dict[str, Any]:
    """Conservatively turn a correction into an evidence update draft.

    This is only a mining aid.  ``manual_required`` remains true whenever the
    wording may depend on the removed assistant turn; curated suites must be
    reviewed by a human before replay.
    """

    original = text.strip()
    if not original:
        return {"replay_text": "", "disposition": "drop_empty", "manual_required": False}
    if len(original) <= 80 and _INTERACTION_ONLY_RE.fullmatch(original):
        return {
            "replay_text": "",
            "disposition": "interaction_failure_signal",
            "manual_required": False,
        }

    stripped = _CORRECTION_PREFIX_RE.sub("", original, count=1).strip(" ，,：:-")
    was_neutralized = stripped != original and len(stripped) >= 4
    replay = f"新增資訊或限制：{stripped}" if was_neutralized else original
    dependent = bool(_DEPENDENT_RE.search(replay))
    return {
        "replay_text": replay,
        "disposition": "neutralized_correction" if was_neutralized else "candidate_update",
        "manual_required": dependent or not was_neutralized,
    }


def draft_from_export(value: object) -> dict[str, Any]:
    export = validate_session_export(value)
    info = export["info"]
    session_id = info["id"]
    user_turns: list[dict[str, Any]] = []
    assistant_count = 0
    user_chars = 0
    for message in export["messages"]:
        role = _message_role(message)
        if role == "assistant":
            assistant_count += 1
            continue
        text = _user_text(message)
        if not text:
            continue
        user_chars += len(text)
        if user_chars > MAX_USER_TEXT_CHARS:
            raise SessionEvalError("session export has too much user-authored text")
        neutral = (
            {
                "replay_text": text,
                "disposition": "initial_prompt",
                "manual_required": False,
            }
            if not user_turns
            else neutralize_followup(text)
        )
        user_turns.append(
            {
                "index": len(user_turns),
                "original_text": text,
                **neutral,
            }
        )
    if not user_turns:
        raise SessionEvalError("session export has no non-empty user text")
    directory = info.get("directory")
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "session_hash": session_hash(session_id),
            "export_digest": json_digest(export),
            "project_root": directory if isinstance(directory, str) else "",
        },
        "user_turns": user_turns,
        "mining": {
            "assistant_messages_excluded": assistant_count,
            "requires_manual_curation": any(turn["manual_required"] for turn in user_turns),
        },
    }


def mine_export_files(paths: Sequence[Path]) -> dict[str, Any]:
    if not paths:
        raise SessionEvalError("mine requires at least one session export")
    if len(paths) > MAX_SESSIONS:
        raise SessionEvalError(f"mine accepts at most {MAX_SESSIONS} sessions")
    drafts = [draft_from_export(read_json_file(path)) for path in paths]
    hashes = [draft["source"]["session_hash"] for draft in drafts]
    if len(hashes) != len(set(hashes)):
        raise SessionEvalError("mine input contains duplicate sessions")
    return {
        "schema_version": SCHEMA_VERSION,
        "source_policy": "user_text_only_assistant_excluded",
        "drafts": drafts,
    }


def _reject_forbidden_oracle_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise SessionEvalError("suite JSON object keys must be strings")
            if key.lower() in FORBIDDEN_ORACLE_KEYS:
                raise SessionEvalError(
                    f"suite key {key!r} is forbidden: model prose is not an oracle"
                )
            _reject_forbidden_oracle_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_forbidden_oracle_keys(child)


def _validate_relative_path(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise SessionEvalError(f"{where} must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise SessionEvalError(f"{where} must stay below project_root")
    return value


def _validate_check(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SessionEvalError(f"{where} must be an object")
    check_type = value.get("type")
    if check_type not in CHECK_TYPES:
        raise SessionEvalError(f"{where}.type is unsupported")
    allowed = {"type"}
    if check_type in {"required_tool", "forbidden_tool"}:
        allowed.add("value")
        if not isinstance(value.get("value"), str) or not value["value"]:
            raise SessionEvalError(f"{where}.value must be a tool name")
    elif check_type in {
        "required_any_tool",
        "required_text_all",
        "required_text_any",
        "forbidden_text",
    }:
        allowed.add("values")
        values = value.get("values")
        if not isinstance(values, list) or not values or not all(
            isinstance(item, str) and item for item in values
        ):
            raise SessionEvalError(f"{where}.values must be a non-empty string array")
    elif check_type == "max_identical_tool_call":
        allowed.add("value")
        if (
            not isinstance(value.get("value"), int)
            or isinstance(value["value"], bool)
            or value["value"] < 1
            or value["value"] > 10
        ):
            raise SessionEvalError(f"{where}.value must be an integer in 1..10")
    unexpected = set(value) - allowed
    if unexpected:
        raise SessionEvalError(f"{where} has unknown fields: {sorted(unexpected)}")
    return value


def validate_suite(value: object) -> dict[str, Any]:
    _reject_forbidden_oracle_keys(value)
    if not isinstance(value, dict):
        raise SessionEvalError("suite root must be an object")
    allowed_top = {"schema_version", "name", "source_policy", "cases"}
    if set(value) - allowed_top:
        raise SessionEvalError(f"suite has unknown fields: {sorted(set(value) - allowed_top)}")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise SessionEvalError(f"suite schema_version must be {SCHEMA_VERSION}")
    if not isinstance(value.get("name"), str) or not value["name"].strip():
        raise SessionEvalError("suite.name must be non-empty")
    if value.get("source_policy") != "user_and_external_evidence_only":
        raise SessionEvalError("suite.source_policy must exclude historical assistant answers")
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > MAX_CASES:
        raise SessionEvalError(f"suite.cases must contain 1..{MAX_CASES} cases")

    seen: set[str] = set()
    for index, case in enumerate(cases):
        where = f"cases[{index}]"
        if not isinstance(case, dict):
            raise SessionEvalError(f"{where} must be an object")
        allowed_case = {
            "id",
            "task_type",
            "project_root",
            "source",
            "turns",
            "read_only",
            "state_paths",
            "verifier",
        }
        if set(case) - allowed_case:
            raise SessionEvalError(f"{where} has unknown fields: {sorted(set(case) - allowed_case)}")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not _SAFE_ID_RE.fullmatch(case_id):
            raise SessionEvalError(f"{where}.id is invalid")
        if case_id in seen:
            raise SessionEvalError(f"duplicate case id: {case_id}")
        seen.add(case_id)
        if case.get("task_type") not in TASK_TYPES:
            raise SessionEvalError(f"{where}.task_type is unsupported")
        project_root = case.get("project_root")
        if not isinstance(project_root, str) or not Path(project_root).is_absolute():
            raise SessionEvalError(f"{where}.project_root must be absolute")
        if case.get("read_only") is not True:
            raise SessionEvalError(f"{where}.read_only must be true for session replay")

        source = case.get("source")
        if not isinstance(source, dict) or set(source) != {
            "session_hash",
            "export_digest",
            "user_turn_indices",
        }:
            raise SessionEvalError(f"{where}.source has an invalid shape")
        if not isinstance(source.get("session_hash"), str) or not _HEX16_RE.fullmatch(
            source["session_hash"]
        ):
            raise SessionEvalError(f"{where}.source.session_hash is invalid")
        if not isinstance(source.get("export_digest"), str) or not _HEX64_RE.fullmatch(
            source["export_digest"]
        ):
            raise SessionEvalError(f"{where}.source.export_digest is invalid")
        turn_indices = source.get("user_turn_indices")
        if not isinstance(turn_indices, list) or not turn_indices or not all(
            isinstance(item, int) and not isinstance(item, bool) and item >= 0
            for item in turn_indices
        ):
            raise SessionEvalError(f"{where}.source.user_turn_indices is invalid")

        turns = case.get("turns")
        if not isinstance(turns, list) or not turns or len(turns) > MAX_TURNS_PER_CASE:
            raise SessionEvalError(f"{where}.turns must contain 1..{MAX_TURNS_PER_CASE} turns")
        first_turn = turns[0]
        if not isinstance(first_turn, dict) or first_turn.get("kind") != "prompt":
            raise SessionEvalError(f"{where}.turns[0] must be the prompt")
        for turn_index, turn in enumerate(turns):
            turn_where = f"{where}.turns[{turn_index}]"
            if not isinstance(turn, dict) or set(turn) != {"kind", "text"}:
                raise SessionEvalError(f"{turn_where} must contain kind and text only")
            if turn.get("kind") not in TURN_KINDS:
                raise SessionEvalError(f"{turn_where}.kind is unsupported")
            text = turn.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TURN_CHARS:
                raise SessionEvalError(f"{turn_where}.text is invalid")

        state_paths = case.get("state_paths", [])
        if not isinstance(state_paths, list):
            raise SessionEvalError(f"{where}.state_paths must be an array")
        for path_index, path in enumerate(state_paths):
            _validate_relative_path(path, f"{where}.state_paths[{path_index}]")

        verifier = case.get("verifier")
        if not isinstance(verifier, dict) or set(verifier) != {
            "oracle_kind",
            "checks",
            "human_dimensions",
        }:
            raise SessionEvalError(f"{where}.verifier has an invalid shape")
        if verifier.get("oracle_kind") not in ORACLE_KINDS:
            raise SessionEvalError(f"{where}.verifier.oracle_kind is unsupported")
        checks = verifier.get("checks")
        if not isinstance(checks, list):
            raise SessionEvalError(f"{where}.verifier.checks must be an array")
        for check_index, check in enumerate(checks):
            _validate_check(check, f"{where}.verifier.checks[{check_index}]")
        dimensions = verifier.get("human_dimensions")
        if not isinstance(dimensions, list) or not all(
            isinstance(item, str) and item.strip() for item in dimensions
        ):
            raise SessionEvalError(f"{where}.verifier.human_dimensions must be a string array")
        if verifier["oracle_kind"] == "human_pairwise" and not dimensions:
            raise SessionEvalError(f"{where} human_pairwise verifier needs review dimensions")
        if verifier["oracle_kind"] == "unscored" and checks:
            raise SessionEvalError(f"{where} unscored verifier cannot silently run checks")
    return value


def suite_digest(value: object) -> str:
    return json_digest(validate_suite(value))


def evaluate_checks(
    checks: Sequence[Mapping[str, Any]],
    turns: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Evaluate deterministic contracts over normalized replay turn records."""

    texts = [str(turn.get("assistant_text") or "") for turn in turns]
    combined = "\n".join(texts)
    calls: list[Mapping[str, Any]] = []
    for turn in turns:
        raw_calls = turn.get("calls")
        if isinstance(raw_calls, list):
            calls.extend(item for item in raw_calls if isinstance(item, Mapping))
    tool_names = [str(call.get("tool") or "") for call in calls]
    call_ids = [str(call.get("identity_digest") or call.get("tool") or "") for call in calls]
    counts = Counter(call_ids)
    results: list[dict[str, Any]] = []

    for check in checks:
        check_type = check["type"]
        passed = False
        if check_type == "terminal":
            passed = bool(turns) and all(
                turn.get("terminal") is True and turn.get("harness_error") is not True
                for turn in turns
            )
        elif check_type == "required_tool":
            passed = check["value"] in tool_names
        elif check_type == "required_any_tool":
            passed = any(value in tool_names for value in check["values"])
        elif check_type == "forbidden_tool":
            passed = check["value"] not in tool_names
        elif check_type == "required_text_all":
            passed = all(value.casefold() in combined.casefold() for value in check["values"])
        elif check_type == "required_text_any":
            passed = any(value.casefold() in combined.casefold() for value in check["values"])
        elif check_type == "forbidden_text":
            passed = not any(value.casefold() in combined.casefold() for value in check["values"])
        elif check_type == "max_identical_tool_call":
            passed = not counts or max(counts.values()) <= check["value"]
        elif check_type == "no_tool_error":
            passed = all(int(turn.get("tool_error_count") or 0) == 0 for turn in turns)
        results.append({"type": check_type, "passed": passed})
    return results


def validate_candidate_result(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SessionEvalError("candidate result root must be an object")
    required = {"schema_version", "suite_digest", "candidate", "cases", "aggregate"}
    if set(value) != required or value.get("schema_version") != SCHEMA_VERSION:
        raise SessionEvalError("candidate result has an invalid top-level shape")
    if not isinstance(value.get("suite_digest"), str) or not _HEX64_RE.fullmatch(
        value["suite_digest"]
    ):
        raise SessionEvalError("candidate result suite_digest is invalid")
    candidate = value.get("candidate")
    if not isinstance(candidate, dict):
        raise SessionEvalError("candidate result candidate must be an object")
    for key in ("label", "model", "fingerprint"):
        if not isinstance(candidate.get(key), str) or not candidate[key]:
            raise SessionEvalError(f"candidate result {key} is invalid")
    if not _HEX64_RE.fullmatch(candidate["fingerprint"]):
        raise SessionEvalError("candidate result fingerprint is invalid")
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SessionEvalError("candidate result cases must be non-empty")
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise SessionEvalError(f"candidate result cases[{index}] must be an object")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or case_id in seen:
            raise SessionEvalError("candidate result case ids are invalid or duplicated")
        seen.add(case_id)
        if not isinstance(case.get("turns"), list):
            raise SessionEvalError(f"candidate result {case_id} turns must be an array")
        if not isinstance(case.get("project_state_digest"), str) or not _HEX64_RE.fullmatch(
            case["project_state_digest"]
        ):
            raise SessionEvalError(f"candidate result {case_id} project state is invalid")
    aggregate = value.get("aggregate")
    if not isinstance(aggregate, dict):
        raise SessionEvalError("candidate result aggregate must be an object")
    if not isinstance(aggregate.get("complete"), bool):
        raise SessionEvalError("candidate result aggregate.complete must be boolean")
    return value


def build_blind_bundle(
    suite_value: object,
    left_value: object,
    right_value: object,
    *,
    random_bytes: bytes | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    suite = validate_suite(suite_value)
    left = validate_candidate_result(left_value)
    right = validate_candidate_result(right_value)
    expected_digest = suite_digest(suite)
    if left["suite_digest"] != expected_digest or right["suite_digest"] != expected_digest:
        raise SessionEvalError("candidate results were not produced from this exact suite")
    if left["candidate"]["fingerprint"] == right["candidate"]["fingerprint"]:
        raise SessionEvalError("blind comparison requires two distinct candidate fingerprints")

    left_cases = {item["case_id"]: item for item in left["cases"]}
    right_cases = {item["case_id"]: item for item in right["cases"]}
    suite_ids = [case["id"] for case in suite["cases"]]
    if set(left_cases) != set(suite_ids) or set(right_cases) != set(suite_ids):
        raise SessionEvalError("candidate result case set does not match the suite")

    entropy = random_bytes if random_bytes is not None else secrets.token_bytes(32)
    if not isinstance(entropy, bytes) or len(entropy) < 16:
        raise SessionEvalError("blind bundle random source must contain at least 16 bytes")
    seed_digest = hashlib.sha256(entropy).hexdigest()
    bundle_cases: list[dict[str, Any]] = []
    key_cases: list[dict[str, Any]] = []
    for case in suite["cases"]:
        case_id = case["id"]
        left_case = left_cases[case_id]
        right_case = right_cases[case_id]
        if left_case["project_state_digest"] != right_case["project_state_digest"]:
            raise SessionEvalError(f"case {case_id} candidates saw different project state")
        bit = hashlib.sha256(entropy + case_id.encode("utf-8")).digest()[0] & 1
        first, second = (left_case, right_case) if bit == 0 else (right_case, left_case)
        first_label, second_label = (
            (left["candidate"]["label"], right["candidate"]["label"])
            if bit == 0
            else (right["candidate"]["label"], left["candidate"]["label"])
        )
        answers = []
        for result_case in (first, second):
            answers.append(
                {
                    "turns": [
                        {"assistant_text": str(turn.get("assistant_text") or "")}
                        for turn in result_case["turns"]
                    ],
                    "automatic_checks": result_case.get("automatic_checks", []),
                }
            )
        bundle_cases.append(
            {
                "case_id": case_id,
                "task_type": case["task_type"],
                "turns": case["turns"],
                "human_dimensions": case["verifier"]["human_dimensions"],
                "answer_a": answers[0],
                "answer_b": answers[1],
                "review": {"choice": None, "reason_tags": [], "notes": ""},
            }
        )
        key_cases.append({"case_id": case_id, "a": first_label, "b": second_label})

    bundle = {
        "schema_version": SCHEMA_VERSION,
        "suite_digest": expected_digest,
        "blind_seed_digest": seed_digest,
        "choices": ["a", "b", "tie", "both_bad"],
        "cases": bundle_cases,
    }
    key = {
        "schema_version": SCHEMA_VERSION,
        "suite_digest": expected_digest,
        "blind_seed_digest": seed_digest,
        "candidates": [left["candidate"], right["candidate"]],
        "cases": key_cases,
    }
    return bundle, key


def render_review_markdown(bundle: Mapping[str, Any]) -> str:
    lines = [
        "# CodeTrail session model blind review",
        "",
        "每題只比較品質，不猜模型。選擇：`a` / `b` / `tie` / `both_bad`。",
        "請同時填同目錄的 `review.json`；本 Markdown 方便閱讀。",
        "",
    ]
    for case in bundle.get("cases", []):
        lines.extend([f"## {case['case_id']} ({case['task_type']})", ""])
        dimensions = case.get("human_dimensions", [])
        if dimensions:
            lines.extend(["評估面向：" + "、".join(dimensions), ""])
        for index, turn in enumerate(case.get("turns", []), start=1):
            lines.extend([f"### User {index} ({turn['kind']})", "", turn["text"], ""])
            for label, answer_key in (("A", "answer_a"), ("B", "answer_b")):
                answer_turns = case[answer_key].get("turns", [])
                answer = answer_turns[index - 1]["assistant_text"] if index <= len(answer_turns) else ""
                lines.extend([f"### Answer {label}.{index}", "", answer or "（空回答）", ""])
        lines.extend(
            [
                "選擇：`未填`",
                "",
                "原因標籤：",
                "",
                "備註：",
                "",
                "---",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
