"""Bounded, per-turn evidence checks for the client's tool loop.

Every observation is a fresh tool result.  This module neither runs tools nor
reuses their output, and retains only digests.  A changed result for the same
call is progress even when its contents return to an older value.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import repeat_guard


_REPEAT_NEXT = (
    "next: Do not repeat the same call; use the existing result or change tool/path/pattern."
)
_CONTEXT_RISK = "context_risk: explicit max_chars exceeds the default 12% context budget\n"
_BANNER_START = "⚠ [重複呼叫偵測] 這是你第 "
_SOURCE_LINE = re.compile(r"^.+?:[1-9][0-9]*:(.*)$")
_CONTEXT_LINE = re.compile(r"^.+?-[1-9][0-9]*-")
_PYTHON_BLOCK = re.compile(r"^--- .+?:([1-9][0-9]*) ---$")
_PYTHON_LINE = re.compile(r"^([> ]) *([1-9][0-9]*)\|(.*)$")


def _digest(value: Any) -> bytes:
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).digest()


def _without_banner(name: str, body: str) -> tuple[str, bool]:
    if not body.startswith(_BANNER_START):
        return body, False
    count, separator, _rest = body[len(_BANNER_START):].partition(" 次用完全相同的參數呼叫 ")
    if not separator or not 1 <= len(count) <= 20 or not count.isascii() or not count.isdecimal():
        return body, False
    expected = repeat_guard.banner(name, int(count))
    if not body.startswith(expected):
        return body, False
    return body[len(expected):], True


def _rendered_body(name: str, text: str) -> tuple[str, str | None, bool]:
    """Recognise only the leading adapter envelope and the exact server banner.

    The original full text has its own digest.  The body digest is used instead
    only when a real repeat banner changed the adapter's status/next fields;
    ordinary changes to those fields remain meaningful.
    """
    body = text
    adapter_status = None
    next_line = ""
    first, separator, rest = text.partition("\n")
    if separator and first in ("status: ok", "status: partial", "status: error"):
        adapter_status = first.removeprefix("status: ")
        body = rest
        if adapter_status != "ok" and body.startswith("next: "):
            next_line, separator, body = body.partition("\n")
            if not separator:
                return text, None, False
    risk = ""
    if adapter_status is not None and body.startswith(_CONTEXT_RISK):
        risk, body = _CONTEXT_RISK, body[len(_CONTEXT_RISK):]
    # Only this exact status/next pair is added by the adapter for RepeatGuard.
    if adapter_status is None or (adapter_status == "partial" and next_line == _REPEAT_NEXT):
        body, repeated = _without_banner(name, body)
    else:
        repeated = False
    return risk + body, adapter_status, repeated


def _grep_evidence(arguments: Mapping[str, Any], body: str) -> tuple[Any, ...] | None:
    """Recognise complete rg/Python grep output; do not infer unknown formats."""
    if set(arguments) - {"pattern", "path", "include", "context"}:
        return None
    pattern = arguments.get("pattern")
    path = arguments.get("path", ".")
    include = arguments.get("include")
    context = arguments.get("context", 0)
    if not isinstance(pattern, str) or "\n" in pattern or "\r" in pattern:
        return None
    if path is not None and not isinstance(path, str):
        return None
    if include is not None and not isinstance(include, str):
        return None
    if type(context) is not int or not 0 <= context <= 5:
        return None
    header, separator, source_body = body.partition("\n")
    if not separator:
        return None
    renderer = ""
    count = 0
    for candidate, suffix in (("rg", " matches) ==="), ("grep", " 結果) ===")):
        prefix = f"=== {candidate} '{pattern}' ("
        if header.startswith(prefix) and header.endswith(suffix):
            raw_count = header[len(prefix):-len(suffix)]
            if 1 <= len(raw_count) <= 20 and raw_count.isascii() and raw_count.isdecimal():
                renderer, count = candidate, int(raw_count)
            break
    if (
        not renderer or count < 1 or "…[行過長,已截斷 " in source_body
        or "[Omitted long matching line]" in source_body
        or "[Omitted long context line]" in source_body
    ):
        return None

    lines = source_body.splitlines()
    if not lines:
        return None
    if context == 0:
        complete = len(lines) == count and all(_SOURCE_LINE.match(line) for line in lines)
        if complete and renderer == "grep":
            # The Python fallback clips source text at 100 characters without a
            # marker.  At that boundary the visible output cannot prove fullness.
            complete = all(len(_SOURCE_LINE.match(line)[1].removeprefix(" ")) < 100 for line in lines)
    elif renderer == "rg":
        matches = sum(bool(_SOURCE_LINE.match(line)) for line in lines)
        complete = matches == count and all(
            line == "--" or _SOURCE_LINE.match(line) or _CONTEXT_LINE.match(line)
            for line in lines
        )
    else:
        blocks = matched = 0
        anchor = None
        complete = True
        for line in lines:
            block = _PYTHON_BLOCK.fullmatch(line)
            if block:
                if anchor is not None and matched != 1:
                    complete = False
                    break
                anchor, matched = int(block[1]), 0
                blocks += 1
                continue
            source = _PYTHON_LINE.fullmatch(line)
            if anchor is None or source is None:
                complete = False
                break
            if len(source[3].removeprefix(" ")) >= 120:
                # Context rows use a separate, also unmarked 120-character cap.
                complete = False
                break
            if source[1] == ">":
                if int(source[2]) != anchor:
                    complete = False
                    break
                matched += 1
        complete = complete and blocks == count and matched == 1
    if not complete:
        return None
    # These defaults are implemented by grep_code itself.  No path resolution,
    # glob rewriting, or pattern approximation takes place here.
    return (path or ".", include, context, renderer, count, source_body)


@dataclass(frozen=True)
class _Fingerprint:
    full: bytes
    body: bytes
    repeated: bool

    def same_as(self, previous: "_Fingerprint") -> bool:
        return self.full == previous.full or (
            (self.repeated or previous.repeated) and self.body == previous.body
        )


class ToolProgress:
    """Observe batches without caching results or retaining private content.

    An entirely exact-repeat batch requests convergence immediately.  A batch
    containing only known evidence from different grep patterns uses the
    stagnant_steps threshold.  Any new result in a batch prevents convergence.
    """

    def __init__(self, stagnant_steps: int, max_entries: int):
        for name, value in (("stagnant_steps", stagnant_steps), ("max_entries", max_entries)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.stagnant_steps = stagnant_steps
        self.max_entries = max_entries
        # Exact identities and grep-evidence identities share one LRU budget.
        self._entries: OrderedDict[bytes, _Fingerprint | None] = OrderedDict()
        self._stagnant = 0
        self.begin_step()

    def begin_step(self) -> None:
        self._observed = False
        self._progress = False
        self._all_exact = True

    def _remember(self, key: bytes, value: _Fingerprint | None) -> None:
        self._entries[key] = value
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def observe(
        self,
        name: str,
        arguments: Mapping[str, Any],
        text: str,
        *,
        status: str,
        read_only: bool,
        dispatched: bool,
    ) -> None:
        self._observed = True
        if dispatched and read_only is not True:
            # Even an error may follow a partial write.  A denied/invalid call
            # did not dispatch and must not erase evidence of a loop.
            self._entries.clear()
            self._stagnant = 0
            self._progress = True
            self._all_exact = False
            return
        try:
            key = _digest(("exact", name, dict(arguments)))
        except (TypeError, ValueError, RecursionError):
            # The model supplies JSON objects.  Unknown non-JSON callers must
            # not accidentally share evidence through repr() or coercion.
            self._progress = True
            self._all_exact = False
            return
        body, adapter_status, repeated = _rendered_body(name, text)
        current = _Fingerprint(
            full=_digest((status, dispatched, text)),
            body=_digest((status == "denied", dispatched, body)),
            repeated=repeated,
        )
        previous = self._entries.get(key)
        exact = previous is not None and current.same_as(previous)
        near_key = None
        if (
            name == "grep_code" and read_only is True and dispatched
            and status == "completed" and (adapter_status in (None, "ok") or repeated)
        ):
            evidence = _grep_evidence(arguments, body)
            if evidence is not None:
                near_key = _digest(("grep", evidence))
        near = near_key is not None and near_key in self._entries
        if not exact:
            self._all_exact = False
            # A changed result for an existing exact key always wins over the
            # near-match set, including a file changed back to its old contents.
            if previous is not None or not near:
                self._progress = True
        if near_key is not None:
            self._remember(near_key, None)
        self._remember(key, current)

    def finish_step(self) -> bool:
        if not self._observed or self._progress:
            self._stagnant = 0
            return False
        self._stagnant += 1
        return self._all_exact or self._stagnant >= self.stagnant_steps
