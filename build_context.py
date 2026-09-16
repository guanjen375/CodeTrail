"""Read-only reconstruction of explicit compilation profiles.

No compiler, shell, response-file command or build command is ever executed.
The manifest is local, owner-only, and authorizes individual generated inputs
by path AND SHA-256. Unknown preprocessing evidence is never confirmed active.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path

from client_paths import read_private_file, replace_private_file
from runtime_dependencies import require_safe_filesystem

SCHEMA_VERSION = 1
SEMANTICS_VERSION = 1
MANIFEST_NAME = "build-context.json"
ACTIVE, INACTIVE, UNKNOWN = "active", "inactive", "unknown"
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_INCLUDE_DEPTH = 80
MAX_VISITS = 10000
_C_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".C", ".s", ".S"}
_IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")


class BuildContextError(RuntimeError):
    """Invalid metadata or a stale evidence snapshot; never silently unscoped."""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_hash(value) -> str:
    return _digest(json.dumps(value, sort_keys=True, ensure_ascii=False,
                              separators=(",", ":")).encode())


def _relative(root: Path, value: str | Path, directory: str = "") -> str:
    path = Path(value)
    if not path.is_absolute():
        path = root / directory / path
    # Lexical normalization must not follow an untrusted symlink first.
    path = Path(os.path.abspath(path))
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise BuildContextError(f"build input outside project root: {value}") from exc


def _read(root: Path, rel: str, *, limit: int = MAX_SOURCE_BYTES) -> bytes:
    """Bounded root-anchored read; reject links at every component."""
    require_safe_filesystem("build context IO", error_type=BuildContextError)
    rel = _relative(root, rel)
    parts = Path(rel).parts
    if not parts or any(p in (".", "..") for p in parts):
        raise BuildContextError("invalid build input path")
    fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # root was canonicalized once. Walk its ancestors too, so swapping a
        # parent into a symlink cannot redirect the anchored read outside root.
        for part in root.parts[1:] + parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=fd)
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise BuildContextError(f"build input must be a regular file with one link: {rel}")
            if info.st_size > limit:
                raise BuildContextError(f"build input exceeds {limit} bytes: {rel}")
            chunks, size = [], 0
            while block := os.read(file_fd, min(65536, limit + 1 - size)):
                chunks.append(block)
                size += len(block)
                if size > limit:
                    raise BuildContextError(f"build input exceeds {limit} bytes: {rel}")
            after = os.fstat(file_fd)
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise BuildContextError(f"build input changed during read: {rel}")
            return b"".join(chunks)
        finally:
            os.close(file_fd)
    finally:
        os.close(fd)


def _decode(data: bytes, rel: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise BuildContextError(f"build input is not UTF-8: {rel}") from exc


def _tri_and(a, b):
    return False if a is False or b is False else (True if a is True and b is True else None)


def _tri_or(a, b):
    return True if a is True or b is True else (False if a is False and b is False else None)


def _tri_not(value):
    return None if value is None else not value


def _state(value):
    return ACTIVE if value is True else INACTIVE if value is False else UNKNOWN


@dataclass(frozen=True)
class Macro:
    defined: bool | None
    value: str | None = None


@dataclass
class MacroEnv:
    values: dict[str, Macro] = field(default_factory=dict)
    complete: bool = False
    tainted: bool = False

    def get(self, name):
        # -dM dumps omit dynamic/function builtins (__LINE__, __has_include,
        # etc.). Absence of a reserved builtin never proves it undefined.
        return self.values.get(name, Macro(False if self.complete and not name.startswith("__") else None))

    def copy(self):
        return MacroEnv(dict(self.values), self.complete, self.tainted)

    def poison(self):
        self.values.clear()
        self.complete = False
        self.tainted = True

    def define(self, text):
        match = re.match(r"([A-Za-z_]\w*)(.*)\Z", text.strip(), re.S)
        if not match:
            self.poison()
            return False
        name, tail = match.groups()
        if name == "defined":
            self.poison()
            return False
        self.values[name] = Macro(True, None if tail.startswith("(") else tail.strip())
        return True


def _merge_envs(envs: list[MacroEnv]) -> MacroEnv:
    if not envs:
        return MacroEnv()
    merged = MacroEnv(complete=all(e.complete for e in envs), tainted=any(e.tainted for e in envs))
    for name in set().union(*(set(e.values) for e in envs)):
        values = [e.get(name) for e in envs]
        merged.values[name] = values[0] if all(v == values[0] for v in values) else Macro(None)
    return merged


_TOKENS = re.compile(
    r"\s*(defined\b|0[xX][0-9a-fA-F]+[uUlL]*|0[bB][01]+[uUlL]*|"
    r"[0-9]+[uUlL]*|[A-Za-z_]\w*|'(?:\\.|[^'\\])*'|"
    r"\|\||&&|<<|>>|<=|>=|==|!=|[()?:!~+*/%<>&^|\-])"
)
_PREC = {"||": 1, "&&": 2, "|": 3, "^": 4, "&": 5, "==": 6, "!=": 6,
         "<": 7, "<=": 7, ">": 7, ">=": 7, "<<": 8, ">>": 8,
         "+": 9, "-": 9, "*": 10, "/": 10, "%": 10}


def _tokenize(expression):
    out, pos = [], 0
    expression = expression.strip()
    while pos < len(expression):
        match = _TOKENS.match(expression, pos)
        if not match or len(out) >= 4096:
            raise ValueError("unsupported preprocessor expression")
        out.append(match[1])
        pos = match.end()
    return out


def _expand(tokens, env, seen=frozenset()):
    out, i = [], 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if token == "defined":
            paren = i < len(tokens) and tokens[i] == "("
            if paren:
                i += 1
            if i >= len(tokens) or not _IDENT.fullmatch(tokens[i]):
                raise ValueError("invalid defined operand")
            defined = env.get(tokens[i]).defined
            i += 1
            if paren:
                if i >= len(tokens) or tokens[i] != ")":
                    raise ValueError("invalid defined operand")
                i += 1
            out.append("?unknown" if defined is None else str(int(defined)))
        elif _IDENT.fullmatch(token):
            macro = env.get(token)
            if macro.defined is False:
                out.append("0")
            elif macro.value is None or token in seen or len(seen) > 50:
                out.append("?unknown")
            else:
                out.extend(_expand(_tokenize(macro.value), env, seen | {token}))
        else:
            out.append(token)
        if len(out) > 8192:
            raise ValueError("macro expansion limit")
    return out


def _bounded(value):
    return value if value is None or -(1 << 63) <= value < (1 << 63) else None


class _Expression:
    def __init__(self, tokens):
        self.tokens, self.pos = tokens, 0

    def pop(self):
        token = self.peek()
        self.pos += 1
        return token

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else ""

    def parse(self, minimum=0):
        token = self.pop()
        if token in ("!", "~", "+", "-"):
            value = self.parse(11)
            left = None if value is None else _bounded(
                int(not value) if token == "!" else ~value if token == "~"
                else value if token == "+" else -value)
        elif token == "(":
            left = self.parse()
            if self.pop() != ")":
                raise ValueError("unbalanced expression")
        elif token == "?unknown":
            left = None
        elif token.startswith("'"):
            char = ast.literal_eval(token)
            left = ord(char) if len(char) == 1 and ord(char) < 128 else None
        elif re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|0[bB][01]+|[0-9]+)[uUlL]*", token):
            # Unsigned arithmetic has different promotion/overflow rules. Do not
            # evaluate it as Python's signed, unbounded integers.
            if "u" in token.lower():
                left = None
            else:
                raw = token.rstrip("lL")
                base = 16 if raw.lower().startswith("0x") else 2 if raw.lower().startswith("0b") else 8 if raw.startswith("0") and len(raw) > 1 else 10
                left = _bounded(int(raw, base))
        else:
            raise ValueError("unsupported expression operand")
        while self.peek() in _PREC and _PREC[self.peek()] >= minimum:
            op = self.pop()
            right = self.parse(_PREC[op] + 1)
            if op == "&&":
                value = _tri_and(None if left is None else bool(left), None if right is None else bool(right))
                left = None if value is None else int(value)
            elif op == "||":
                value = _tri_or(None if left is None else bool(left), None if right is None else bool(right))
                left = None if value is None else int(value)
            elif left is None or right is None:
                left = None
            elif op in ("/", "%"):
                if right == 0:
                    left = None
                else:
                    quotient = (abs(left) // abs(right)) * (-1 if (left < 0) != (right < 0) else 1)
                    left = _bounded(quotient if op == "/" else left - quotient * right)
            elif op in ("<<", ">>"):
                left = None if left < 0 or not 0 <= right < 63 else _bounded(left << right if op == "<<" else left >> right)
            else:
                left = _bounded({"+": lambda: left + right, "-": lambda: left - right,
                    "*": lambda: left * right, "&": lambda: left & right,
                    "|": lambda: left | right, "^": lambda: left ^ right,
                    "<": lambda: int(left < right), "<=": lambda: int(left <= right),
                    ">": lambda: int(left > right), ">=": lambda: int(left >= right),
                    "==": lambda: int(left == right), "!=": lambda: int(left != right)}[op]())
        if minimum == 0 and self.peek() == "?":
            self.pop()
            yes = self.parse()
            if self.pop() != ":":
                raise ValueError("invalid ternary expression")
            no = self.parse()
            left = yes if left else no if left is not None else yes if yes == no else None
        return left


def evaluate_condition(expression: str, env: MacroEnv) -> bool | None:
    """A small conservative C preprocessor integer-expression subset."""
    try:
        parser = _Expression(_expand(_tokenize(expression), env))
        result = parser.parse()
        return None if parser.pos != len(parser.tokens) or result is None else bool(result)
    except (ValueError, SyntaxError, TypeError, RecursionError, OverflowError):
        return None


def _shell_words(command: str) -> tuple[list[str], bool]:
    # Parsing is data processing only; expansions and compound commands remain
    # unknown even if a recognizable compiler invocation can be recovered.
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError as exc:
        raise BuildContextError(f"malformed compiler command: {exc}") from exc
    opaque = any(re.fullmatch(r"[;&|<>()]+", word) or "$" in word or "`" in word for word in words)
    return words, opaque


def _compiler_index(words):
    for i, word in enumerate(words):
        name = Path(word).name
        if re.fullmatch(r"(?:[\w.+-]+-)?(?:gcc|g\+\+|clang|clang\+\+|cc|c\+\+)(?:-[0-9.]+)?|ccac|ccarc|mcc|ccrv", name):
            return i
    return None


def _response_words(root, directory, words, records, issues, *, metaware=False, depth=0):
    out = []
    for word in words:
        if not word.startswith("@"):
            out.append(word)
            continue
        try:
            if depth >= 8:
                raise BuildContextError("response nesting limit")
            rel = _relative(root, word[1:], directory)
            data = _read(root, rel, limit=MAX_INPUT_BYTES)
            records[rel] = {"path": rel, "sha256": _digest(data), "kind": "response"}
            content = _decode(data, rel)
            if metaware:
                content = "\n".join(line for line in content.splitlines() if not line.startswith("!"))
            # Response files use argument quoting, not shell syntax. Metacharacters
            # are literal arguments; they will be rejected as unsupported below.
            nested = shlex.split(content, posix=True)
            out.extend(_response_words(root, directory, nested, records, issues,
                                       metaware=metaware, depth=depth + 1))
        except (OSError, ValueError, BuildContextError) as exc:
            issues.append(f"response unavailable: {word}: {exc}")
    return out


_NO_EFFECT = {"-c", "-S", "-E", "-g", "-g0", "-g1", "-g2", "-g3", "-pipe", "-v", "-w",
              "-M", "-MM", "-MD", "-MMD", "-MP", "-MG", "-nostdlib", "-nodefaultlibs",
              "-fno-common", "-fcommon", "-ffunction-sections", "-fdata-sections",
              "-fno-asynchronous-unwind-tables", "-fno-unwind-tables", "-fno-exceptions",
              "-fno-rtti", "-fno-stack-protector", "-fomit-frame-pointer", "-Wall", "-Wextra",
              "-Werror", "-pedantic", "-pedantic-errors", "-nostdinc", "-nostdinc++",
              "-ffreestanding", "-fhosted", "-fno-builtin", "-fshort-enums", "-fshort-wchar",
              "-fsigned-char", "-funsigned-char", "-pthread", "-fPIC", "-fpic", "-fPIE", "-fpie",
              "-mno-unaligned-access", "-munaligned-access", "-mstrict-align", "-mlong-calls",
              "-mthumb-interwork", "-mgeneral-regs-only", "-mno-outline-atomics",
              "-Hnocopyr", "-Hnosdata", "-Hnocrt", "-Hkeepasm", "-undef"}
_TARGET_FLAGS = re.compile(r"(?:-m(?:cpu|arch|tune|abi|fpu|float-abi)=\S+|-m(?:thumb|arm|big-endian|little-endian|32|64)|-a(?:6|7|v2em|v2hs|rcv2em|rcv2hs)|-H[BL]|-X(?:no)?(?:cd|norm|swap|div_rem|mpy(?:16)?|macd?|qmpyh|ll64|bs|sa|atomic|unaligned|Unaligned|timer[01]|ea)|-core[0-9]+)\Z")


def _normalize_entry(root, entry, origin, responses):
    issues = []
    invalid_directory = False
    try:
        directory = _relative(root, str(entry.get("directory") or root))
    except BuildContextError as exc:
        directory = ""
        invalid_directory = True
        issues.append(str(exc))
    raw_arguments = entry.get("arguments")
    if raw_arguments is not None:
        if not isinstance(raw_arguments, list) or not raw_arguments or any(not isinstance(a, str) or "\0" in a for a in raw_arguments):
            raise BuildContextError("compile entry arguments must be a nonempty string array")
        words, opaque = list(raw_arguments), False
    elif isinstance(entry.get("command"), str):
        words, opaque = _shell_words(entry["command"])
    else:
        raise BuildContextError("compile entry requires arguments or command")
    index = _compiler_index(words)
    compiler = words[index] if index is not None else words[0] if words else ""
    if index is None or (index and any(Path(w).name not in {"ccache", "sccache", "distcc"} for w in words[:index])):
        issues.append("unsupported compiler driver or launcher")
    if opaque:
        issues.append("shell expansion/compound command is not executed or reconstructed")
    words = words[(index or 0) + 1:]
    metaware = Path(compiler).name in {"ccac", "ccarc", "mcc", "ccrv"}
    words = _response_words(root, directory, words, responses, issues, metaware=metaware)
    options, includes, forced, source_candidates = [], [], [], []
    language = None
    output = entry.get("output")
    i = 0
    while i < len(words):
        word = words[i]
        i += 1
        option, value = None, None
        for prefix in ("-isystem", "-iquote", "-idirafter", "-imacros", "-include", "-D", "-U", "-I"):
            if word == prefix:
                if i < len(words):
                    option, value = prefix, words[i]
                    i += 1
                else:
                    issues.append(f"missing argument: {prefix}")
                break
            if word.startswith(prefix) and word != prefix:
                option, value = prefix, word[len(prefix):]
                break
        if word.startswith("-Hinclude="):
            option, value = "-include", word.partition("=")[2]
        if option:
            if option in ("-D", "-U"):
                options.append({"option": option, "value": value})
            elif option in ("-include", "-imacros"):
                forced.append({"option": option, "value": value})
            else:
                if value == "-" or value.startswith("="):
                    issues.append(f"unsupported include search modifier: {option}{value}")
                try:
                    path = _relative(root, value, directory)
                except BuildContextError as exc:
                    path = None
                    issues.append(str(exc))
                includes.append({"option": option, "value": value, "path": path})
            continue
        if word in ("-o", "-MF", "-MT", "-MQ", "-x", "-target", "--target", "-isysroot", "--sysroot"):
            if i >= len(words):
                issues.append(f"missing argument: {word}")
                continue
            value = words[i]
            i += 1
            if word == "-o":
                output = value
            elif word in ("-isysroot", "--sysroot"):
                issues.append(f"implicit sysroot include search unavailable: {value}")
            elif word == "-x" and value not in ("c", "c++", "assembler-with-cpp"):
                issues.append(f"unsupported source language: {value}")
            elif word == "-x":
                language = "cpp" if value == "c++" else value
            continue
        if word.startswith("-o") and len(word) > 2:
            output = word[2:]
        elif word in _NO_EFFECT or re.fullmatch(r"-O(?:[0-3sgz]|fast)?|-g(?:dwarf-[0-9]+)?|-W(?:no-)?[A-Za-z0-9_=-]+", word):
            pass
        elif _TARGET_FLAGS.fullmatch(word) or word.startswith(("-std=", "--target=")):
            pass  # Kept verbatim; target builtins require an explicit captured dump.
        elif word.startswith("--sysroot="):
            issues.append(f"implicit sysroot include search unavailable: {word}")
        elif word.startswith("-"):
            issues.append(f"unsupported compiler flag: {word}")
        elif Path(word).suffix in _C_SUFFIXES:
            source_candidates.append(word)
        elif Path(word).suffix not in {".o", ".obj", ".a", ".lib"}:
            issues.append(f"unsupported compiler argument: {word}")
    source = entry.get("file") or (source_candidates[0] if len(source_candidates) == 1 else None)
    raw_source = source
    if len(source_candidates) > 1:
        issues.append("multiple input source files in compiler invocation")
    try:
        source = _relative(root, source, directory) if isinstance(source, str) else None
    except BuildContextError as exc:
        source = None
        issues.append(str(exc))
    if invalid_directory:
        source = None
    if source is None:
        issues.append("source translation unit unavailable")
    if language is None:
        suffix = Path(source or "").suffix
        language = ("assembler-with-cpp" if suffix == ".S" else "assembler" if suffix == ".s"
                    else "cpp" if ("++" in Path(compiler).name or suffix in {".cc", ".cpp", ".cxx", ".C"}) else "c")
    return {"directory": directory, "directory_raw": entry.get("directory"),
            "source": source, "source_raw": raw_source, "output": output, "language": language,
            "compiler": compiler, "arguments": raw_arguments, "command": entry.get("command"),
            "expanded_arguments": words, "macro_options": options, "include_options": includes,
            "forced_includes": forced, "origin": origin, "issues": issues}


def _log_entries(text, root):
    directory, stack = str(root), []
    entries, unknowns = [], []
    for number, line in enumerate(text.splitlines(), 1):
        match = re.search(r"(?:make(?:\[\d+\])?): (Entering|Leaving) directory ['`](.+)['’]", line)
        if match:
            if match[1] == "Entering":
                stack.append(directory)
                directory = match[2]
            elif stack:
                directory = stack.pop()
            continue
        # Ninja's progress prefix is evidence decoration, not an argument.
        line = re.sub(r"^\[\d+/\d+\]\s*", "", line.strip())
        if not line:
            continue
        try:
            words, opaque = _shell_words(line)
        except BuildContextError:
            unknowns.append(f"log line {number}: malformed command")
            continue
        local_dir = directory
        if len(words) > 3 and words[0] == "cd" and words[2] == "&&":
            local_dir = str(Path(directory) / words[1])
            words = words[3:]
            opaque = any(re.fullmatch(r"[;&|<>()]+", w) or "$" in w or "`" in w for w in words)
        if _compiler_index(words) is None:
            unknowns.append(f"log line {number}: not reconstructed as a supported compiler invocation")
            continue
        entry = {"directory": local_dir, "arguments": words}
        if opaque:
            entry["_log_opaque"] = True
        entries.append((number, entry))
    return entries, unknowns


def _strip_comments(text):
    pattern = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*'
    return re.sub(pattern, lambda m: re.sub(r"[^\n]", " ", m[0]) if m[0].startswith("/") else m[0], text, flags=re.S)


def _logical_lines(text):
    # Splice before removing comments, but retain physical line coordinates.
    logical, pending, start = [], "", 1
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        body = line.rstrip("\r\n")
        if not pending:
            start = number
        pending += body[:-1] if body.endswith("\\") else body
        if not body.endswith("\\"):
            logical.append((start, number, pending))
            pending = ""
    if pending:
        logical.append((start, number, pending))
    cleaned = _strip_comments("\n".join(row[2] for row in logical)).split("\n")
    return [(a, b, cleaned[i]) for i, (a, b, _) in enumerate(logical)]


@dataclass
class BuildContext:
    root: Path
    target: str | None = None
    profile: dict | None = None
    manifest_hash: str | None = None
    available_targets: tuple[str, ...] = ()
    issues: list[str] = field(default_factory=list)
    dependencies: dict[str, str | None] = field(default_factory=dict)
    line_states: dict[str, dict[int, list[bool | None]]] = field(default_factory=dict)
    include_records: list[dict] = field(default_factory=list)
    source_paths: set[str] = field(default_factory=set)
    admitted_files: dict[str, str] = field(default_factory=dict)
    file_units: dict[str, set[str]] = field(default_factory=dict)
    file_languages: dict[str, set[str]] = field(default_factory=dict)
    fingerprint: str = ""
    _contents: dict[str, str] = field(default_factory=dict, repr=False)
    _unavailable_paths: set[str] = field(default_factory=set, repr=False)
    _visit_count: int = 0
    _importing: bool = False

    @property
    def restricts_files(self):
        return self.profile is not None and self.target is not None

    def _issue(self, message):
        if message not in self.issues:
            self.issues.append(message)

    def summary(self):
        return {"target": self.target, "variant": (self.profile or {}).get("variant"),
                "status": UNKNOWN if self.issues or not self.restricts_files else ACTIVE,
                "fingerprint": self.fingerprint, "available_targets": list(self.available_targets),
                "translation_units": len(self.source_paths), "unknowns": self.issues[:100],
                "unknown_count": len(self.issues), "unknowns_truncated": len(self.issues) > 100}

    def _input(self, rel, kind="header"):
        if rel in self._contents:
            return self._contents[rel]
        try:
            data = _read(self.root, rel)
            digest = _digest(data)
            previous = self.dependencies.get(rel)
            if previous is not None and previous != digest:
                raise BuildContextError(f"build input changed during reconstruction: {rel}")
            self.dependencies[rel] = digest
            from utils import should_ignore_dir
            ignored = should_ignore_dir(Path(rel).parent)
            if ignored and kind == "header":
                if not self._importing and self.admitted_files.get(rel) != digest:
                    self._issue(f"generated header not admitted by manifest/hash: {rel}; reimport profile")
                    return None
                self.admitted_files[rel] = digest
            content = _decode(data, rel)
            self._contents[rel] = content
            return content
        except FileNotFoundError:
            self.dependencies.setdefault(rel, None)
            return None
        except (OSError, BuildContextError) as exc:
            self._unavailable_paths.add(rel)
            self._issue(f"unavailable {kind} {rel}: {exc}")
            return None

    def _include(self, raw, angle, path, entry, *, forced=False):
        directories = [] if angle else [entry["directory"] if forced else str(Path(path).parent)]
        system_paths = {value["path"] for value in entry["include_options"]
                        if value["option"] in ("-isystem", "-idirafter")}
        for group in (("-iquote",) if not angle else ()) + ("-I", "-isystem", "-idirafter"):
            # The compiler searches quote, -I, system, after groups in that order;
            # each group's order is exactly the command-line order.
            directories.extend(v["path"] for v in entry["include_options"] if v["option"] == group
                               and not (group == "-I" and v["path"] in system_paths))
        if Path(raw).is_absolute():
            directories = [""]
        for directory in directories:
            if directory is None:
                self._issue(f"unknown external include search before {raw}")
                return None
            try:
                rel = _relative(self.root, raw, directory)
            except BuildContextError as exc:
                self._issue(str(exc))
                return None
            content = self._input(rel)
            if content is not None:
                return rel
            # An existing but unreadable / unadmitted candidate shadows later
            # include paths; never silently choose a lower-priority header.
            if self.dependencies.get(rel) is not None or rel in self._unavailable_paths:
                return None
        self._issue(f"missing include {raw} from {path}; implicit compiler include paths unavailable")
        return None

    def _record(self, path, start, end, state, unit):
        self.file_units.setdefault(path, set()).add(unit)
        lines = self.line_states.setdefault(path, {})
        for number in range(start, end + 1):
            values = lines.setdefault(number, [])
            if state not in values:
                values.append(state)

    def _preprocess(self, path, env, entry, unit, active=True, stack=(), once=None, macros_only=False):
        once = set() if once is None else once
        if path in once:
            return env
        self._visit_count += 1
        if len(stack) >= MAX_INCLUDE_DEPTH or self._visit_count > MAX_VISITS or path in stack:
            self._issue(f"recursive/oversized include graph: {path}")
            env.poison()
            return env
        content = self._input(path, "source" if path in self.source_paths else "header")
        if content is None:
            self._issue(f"missing source/header: {path}")
            env.poison()
            return env
        self.file_languages.setdefault(path, set()).add(entry.get("language", "c"))
        if re.search(r'\b(?:u8|u|U|L)?R"', content) or "??/" in content:
            self._issue(f"unsupported raw-string/trigraph preprocessing: {path}")
            self._record(path, 1, len(content.splitlines()), None, unit)
            env.poison()
            return env
        frames, current = [], active
        for start, end, text in _logical_lines(content):
            match = re.match(r"\s*#\s*([A-Za-z_]\w*)\s*(.*)", text)
            command, arg = match.groups() if match else ("", "")
            if command in ("if", "ifdef", "ifndef"):
                expression = arg if command == "if" else f"defined({arg.strip()})"
                condition = None if entry.get("issues") else evaluate_condition(expression, env)
                if command == "ifndef":
                    condition = _tri_not(condition)
                if condition is None and current is not False:
                    self._issue(f"unknown condition: {path}:{start}: {command} {arg}")
                frames.append({"parent": current, "base": env.copy(), "remaining": _tri_not(condition),
                               "branches": [], "branch": condition, "else": False})
                env = env.copy()
                self._record(path, start, end, current, unit)
                current = _tri_and(current, condition)
                continue
            if command in ("elif", "else", "endif"):
                if not frames:
                    self._issue(f"unbalanced conditional: {path}:{start}")
                    env.poison()
                    current = None
                    continue
                frame = frames[-1]
                if frame["branch"] is not False:
                    frame["branches"].append(env.copy())
                self._record(path, start, end, frame["parent"], unit)
                if command == "endif":
                    if frame["remaining"] is not False:
                        frame["branches"].append(frame["base"])
                    env = _merge_envs(frame["branches"])
                    current = frame["parent"]
                    frames.pop()
                else:
                    env = frame["base"].copy()
                    if frame["else"]:
                        self._issue(f"duplicate else/elif after else: {path}:{start}")
                        env.poison()
                        condition = None
                    else:
                        condition = True if command == "else" else None if entry.get("issues") else evaluate_condition(arg, env)
                    if condition is None and frame["parent"] is not False:
                        self._issue(f"unknown condition: {path}:{start}: {command} {arg}")
                    frame["branch"] = _tri_and(frame["remaining"], condition)
                    frame["remaining"] = _tri_and(frame["remaining"], _tri_not(condition))
                    frame["else"] = command == "else"
                    current = _tri_and(frame["parent"], frame["branch"])
                continue
            recorded = False if macros_only and not command else _tri_and(current, None if env.tainted else True)
            self._record(path, start, end, recorded, unit)
            if current is False:
                continue
            if command == "define":
                if not env.define(arg):
                    self._issue(f"unsupported define: {path}:{start}")
            elif command == "undef":
                if _IDENT.fullmatch(arg.strip()):
                    env.values[arg.strip()] = Macro(False)
                else:
                    env.poison()
                    self._issue(f"unsupported undef: {path}:{start}")
            elif command == "include":
                raw = arg.strip()
                for _ in range(20):
                    if not _IDENT.fullmatch(raw):
                        break
                    value = env.get(raw).value
                    if value is None or value == raw:
                        break
                    raw = value.strip()
                include_match = re.fullmatch(r'"([^"\n]+)"|<([^>\n]+)>', raw)
                if include_match:
                    target = self._include(include_match[1] or include_match[2], bool(include_match[2]), path, entry)
                    self.include_records.append({"path": path, "line": start, "raw": arg.strip(),
                        "target": target, "angle": bool(include_match[2]),
                        "state": _state(_tri_and(current, None if env.tainted else True)), "unit": unit})
                    if target:
                        env = self._preprocess(target, env, entry, unit, current, stack + (path,), once,
                                               macros_only=macros_only)
                    else:
                        env.poison()
                else:
                    self.include_records.append({"path": path, "line": start, "raw": arg.strip(),
                        "target": None, "angle": False, "state": UNKNOWN, "unit": unit})
                    env.poison()
                    self._issue(f"unsupported include expression: {path}:{start}")
            elif command == "pragma" and arg.strip() == "once" and current is True:
                once.add(path)
            elif command in ("include_next", "import", "error") or (command == "pragma" and re.search(r"\b(?:push_macro|pop_macro)\b", arg)):
                env.poison()
                current = None
                self._issue(f"unsupported preprocessing directive: {path}:{start}: {command}")
        if frames:
            self._issue(f"unterminated conditional: {path}")
            # Malformed input cannot certify any earlier branch in this file.
            self.line_states[path] = {n: [None] for n in self.line_states.get(path, {})}
            env.poison()
        return env

    def reconstruct(self):
        if not self.restricts_files:
            self.fingerprint = _json_hash({"schema": SCHEMA_VERSION, "manifest": self.manifest_hash, "target": self.target})
            return self
        entries = self.profile["entries"]
        self.source_paths = {e["source"] for e in entries if e.get("source")}
        duplicates = {p for p in self.source_paths if sum(e.get("source") == p for e in entries) > 1}
        baseline = MacroEnv()
        builtin = self.profile.get("builtin_macros")
        if builtin:
            text = self._input(builtin["path"], "builtin macro dump")
            if text is not None:
                baseline.complete = bool(text.strip())
                for _a, _b, line in _logical_lines(text):
                    if not line.strip():
                        continue
                    match = re.match(r"\s*#\s*define\s+(.*)", line)
                    if match:
                        if not baseline.define(match[1]):
                            self._issue("invalid definition in builtin macro dump")
                    else:
                        baseline.complete = False
                        self._issue("unsupported builtin macro dump; absence of a macro is unknown")
                for name in ("__LINE__", "__FILE__", "__COUNTER__", "__DATE__", "__TIME__",
                             "__TIMESTAMP__", "__BASE_FILE__", "__INCLUDE_LEVEL__"):
                    if name in baseline.values:
                        baseline.values[name] = Macro(True)
        if not baseline.complete:
            self._issue("compiler builtin macros unavailable/incomplete; unspecified macros are unknown")
        for index, entry in enumerate(entries):
            source = entry.get("source")
            self.issues.extend(i for i in entry.get("issues", []) if i not in self.issues)
            if not source:
                continue
            env = baseline.copy()
            for option in entry["macro_options"]:
                value = option["value"]
                if option["option"] == "-U":
                    if _IDENT.fullmatch(value):
                        env.values[value] = Macro(False)
                    else:
                        env.poison()
                        self._issue(f"unsupported -U operand: {value}")
                else:
                    name, sep, replacement = value.partition("=")
                    if not env.define(f"{name} {replacement if sep else '1'}"):
                        self._issue(f"unsupported -D operand: {value}")
            uncertain = bool(entry.get("issues")) or source in duplicates
            if source in duplicates:
                self._issue(f"ambiguous translation unit entries: {source}; select explicit --entry-output")
            if entry.get("issues"):
                env.poison()
            unit = f"{index}:{source}"
            # GCC processes all -D/-U before imacros, then include files.
            once = set()
            for forced in sorted(enumerate(entry["forced_includes"]), key=lambda pair: (pair[1]["option"] != "-imacros", pair[0])):
                spec = forced[1]
                path = self._include(spec["value"], False, source, entry, forced=True)
                self.include_records.append({"path": source, "line": 0, "raw": spec["value"],
                    "target": path, "angle": False, "forced": True, "unit": unit,
                    "state": UNKNOWN if uncertain or env.tainted or path is None else ACTIVE})
                if path:
                    env = self._preprocess(path, env, entry, unit, None if uncertain else True,
                                           once=once, macros_only=spec["option"] == "-imacros")
                else:
                    env.poison()
            self._preprocess(source, env, entry, unit, None if uncertain else True, once=once)
        for path, languages in self.file_languages.items():
            if len(languages) > 1:
                self._issue(f"ambiguous source language contexts: {path}")
                self.line_states[path] = {n: [False if all(v is False for v in values) else None]
                                         for n, values in self.line_states[path].items()}
        self.fingerprint = _json_hash({"schema": SCHEMA_VERSION, "semantics": SEMANTICS_VERSION,
            "target": self.target, "profile": self.profile, "manifest": self.manifest_hash,
            "dependencies": self.dependencies, "unavailable_paths": sorted(self._unavailable_paths),
            "admitted": self.admitted_files})
        return self

    def assert_fresh(self):
        if not self.restricts_files:
            return
        if self.manifest_hash is not None:
            data = read_private_file(self.root / ".codetrail", MANIFEST_NAME, BuildContextError,
                                     max_bytes=MAX_INPUT_BYTES, anchor=self.root)
            if data is None or _digest(data) != self.manifest_hash:
                raise BuildContextError("build profile changed during query; reload build context")
        for rel, expected in self.dependencies.items():
            try:
                actual = _digest(_read(self.root, rel, limit=MAX_INPUT_BYTES))
            except FileNotFoundError:
                actual = None
            except OSError as exc:
                raise BuildContextError(f"build dependency unavailable: {rel}: {exc}") from exc
            if actual != expected:
                raise BuildContextError(f"build dependency changed: {rel}; reload/reimport build context")

    def allows_file(self, path):
        return not self.restricts_files or str(path).replace("\\", "/") in self.line_states

    def parser_language(self, path):
        languages = self.file_languages.get(path, set())
        return "cpp" if "cpp" in languages else "c" if "c" in languages else None

    def admits_generated(self, path):
        expected = self.admitted_files.get(path)
        if not expected or path in self.source_paths or not self.allows_file(path):
            return False
        try:
            return _digest(_read(self.root, path)) == expected
        except (OSError, BuildContextError):
            return False

    def state_for(self, path, line=1, end_line=None):
        if not self.restricts_files:
            return UNKNOWN
        path = str(path).replace("\\", "/")
        if path not in self.line_states:
            return INACTIVE
        values = self.line_states[path].get(int(line), [None])
        # A header can have different states in different translation units.
        # Only unanimous evidence is called active/inactive.
        if all(v is False for v in values):
            return INACTIVE
        if all(v is True for v in values):
            return ACTIVE
        return UNKNOWN

    def allows_item(self, item):
        return self.state_for(item.get("path", ""), item.get("line", item.get("start_line", 1))) != INACTIVE

    def mask_source(self, path, content):
        """Blank only proven-inactive physical lines; preserve all coordinates."""
        if not self.restricts_files:
            return content
        return "".join(re.sub(r"[^\r\n]", " ", text) if self.state_for(path, n) == INACTIVE else text
                       for n, text in enumerate(content.splitlines(keepends=True), 1))

    def read_source(self, path):
        if path not in self._contents:
            raise BuildContextError(f"source not in selected context: {path}")
        return self._contents[path]

    def filter_window(self, path, text):
        if not self.restricts_files:
            return text
        out = []
        for line in text.splitlines(keepends=True):
            match = re.match(r"\s*(\d+)\s*[:|]\s?(.*)", line)
            if match and self.state_for(path, int(match[1])) == INACTIVE:
                continue
            out.append(line)
        return "".join(out)

    def include_targets(self, path, line, raw=None):
        return sorted({r["target"] for r in self.include_records if r["path"] == path and
                       r["line"] == line and (raw is None or r["raw"] == raw) and
                       r["target"] and r["state"] != INACTIVE})

    def include_state(self, path, line, target):
        records = [r for r in self.include_records if r["path"] == path and r["line"] == line
                   and r["target"] == target]
        if not records or any(r["state"] != ACTIVE for r in records):
            return UNKNOWN
        return ACTIVE if line == 0 else self.state_for(path, line)


def _load_manifest(root):
    raw = read_private_file(root / ".codetrail", MANIFEST_NAME, BuildContextError,
                            max_bytes=MAX_INPUT_BYTES, anchor=root)
    if raw is None:
        return None, None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise BuildContextError("invalid build context manifest JSON") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION or value.get("root") != str(root) or not isinstance(value.get("targets"), dict):
        raise BuildContextError("build context schema/root mismatch; reimport")
    return value, _digest(raw)


def load_build_context(root: str | Path, target: str | None = None) -> BuildContext:
    root = Path(root).resolve()
    manifest, digest = _load_manifest(root)
    context = BuildContext(root=root, target=target or None, manifest_hash=digest)
    if manifest is None:
        if target:
            raise BuildContextError(f"build target {target!r} has no imported metadata; import this target first")
        context._issue("build metadata unavailable; search is unscoped and target is unknown")
        return context.reconstruct()
    context.available_targets = tuple(sorted(manifest["targets"]))
    if not target:
        context._issue("no explicit build target selected; search is unscoped and target is unknown")
        return context.reconstruct()
    if target not in manifest["targets"]:
        raise BuildContextError(f"unknown build target {target!r}; available: {', '.join(context.available_targets)}")
    profile = manifest["targets"][target]
    if not isinstance(profile, dict) or not isinstance(profile.get("entries"), list):
        raise BuildContextError("invalid build profile entries; reimport")
    context.profile = profile
    for record in profile.get("inputs", []) + profile.get("admitted_files", []) + ([profile["builtin_macros"]] if profile.get("builtin_macros") else []):
        if not isinstance(record, dict) or not isinstance(record.get("path"), str) or not re.fullmatch(r"[a-f0-9]{64}", str(record.get("sha256", ""))):
            raise BuildContextError("invalid build input identity; reimport")
        try:
            actual = _digest(_read(root, record["path"], limit=MAX_INPUT_BYTES))
        except (OSError, BuildContextError) as exc:
            raise BuildContextError(f"build profile input unavailable: {record['path']}; reimport") from exc
        if actual != record["sha256"]:
            raise BuildContextError(f"build profile input changed: {record['path']}; reimport")
        context.dependencies[record["path"]] = actual
        if record.get("kind") == "header":
            context.admitted_files[record["path"]] = actual
    context.issues.extend(profile.get("issues", []))
    context.reconstruct()
    context.assert_fresh()
    return context


def import_build_context(root: str | Path, target: str, *, compile_commands=None,
                         build_log=None, variant=None, entry_outputs=(), builtin_macros=None,
                         generated_headers=()) -> dict:
    """Import one explicitly named target, preserving other target profiles."""
    root = Path(root).resolve()
    if not isinstance(target, str) or not target.strip() or len(target) > 200 or any(ord(c) < 32 for c in target):
        raise BuildContextError("target must be an explicit nonempty label (up to 200 characters)")
    if not compile_commands and not build_log:
        raise BuildContextError("provide compile_commands and/or a verbose build log")
    inputs, entries, issues, responses = [], [], [], {}
    for path, kind in ((compile_commands, "compile_commands"), (build_log, "build_log")):
        if not path:
            continue
        rel = _relative(root, path)
        data = _read(root, rel, limit=MAX_INPUT_BYTES)
        inputs.append({"path": rel, "sha256": _digest(data), "kind": kind})
        text = _decode(data, rel)
        if kind == "compile_commands":
            try:
                values = json.loads(text)
            except ValueError as exc:
                raise BuildContextError("invalid compilation database JSON") from exc
            if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
                raise BuildContextError("compilation database must be an array of objects")
            raw_entries = list(enumerate(values, 1))
        else:
            raw_entries, log_issues = _log_entries(text, root)
            issues.extend(log_issues)
        for number, value in raw_entries:
            if variant is not None and "variant" in value and value["variant"] != variant:
                continue
            if "target" in value and value["target"] != target:
                continue
            entry_responses = {}
            entry = _normalize_entry(root, value, {"path": rel, "entry_or_line": number}, entry_responses)
            if value.get("_log_opaque"):
                entry["issues"].append("compound/expanded log command is not reconstructed")
            if entry_outputs and entry.get("output") not in entry_outputs:
                continue
            responses.update(entry_responses)
            entries.append(entry)
    if not entries:
        raise BuildContextError("no matching compiler entries; check explicit target/output selection")
    profile = {"variant": variant, "inputs": inputs, "entries": entries,
               "admitted_files": list(responses.values()), "issues": issues}
    if builtin_macros:
        rel = _relative(root, builtin_macros)
        data = _read(root, rel)
        profile["builtin_macros"] = {"path": rel, "sha256": _digest(data), "kind": "builtin_macros"}
    context = BuildContext(root, target=target, profile=profile, _importing=True)
    for path in generated_headers:
        rel = _relative(root, path)
        data = _read(root, rel)
        context.admitted_files[rel] = _digest(data)
    context.reconstruct()
    profile["admitted_files"].extend({"path": p, "sha256": h, "kind": "header"}
                                       for p, h in sorted(context.admitted_files.items()))
    import fs_safety
    lock_fd = fs_safety.acquire_file_lock(root / ".code_build_context.lock", root)
    try:
        manifest, _old_digest = _load_manifest(root)
        manifest = manifest or {"schema_version": SCHEMA_VERSION, "root": str(root), "targets": {}}
        manifest["targets"][target] = profile
        # Revalidate after acquiring the writer lock, including any time spent
        # waiting for another profile import. Never publish a partially read TU.
        for record in inputs + profile["admitted_files"] + ([profile["builtin_macros"]] if profile.get("builtin_macros") else []):
            if _digest(_read(root, record["path"], limit=MAX_INPUT_BYTES)) != record["sha256"]:
                raise BuildContextError(f"build input changed during import: {record['path']}")
        context.assert_fresh()
        replace_private_file(root / ".codetrail", MANIFEST_NAME,
                             json.dumps(manifest, ensure_ascii=False, indent=2).encode(),
                             BuildContextError, anchor=root)
    finally:
        fs_safety.release_file_lock(lock_fd)
    return load_build_context(root, target).summary()
