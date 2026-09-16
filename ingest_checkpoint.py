"""Private, content-checked ingestion units, independent of figure retention.

No model inference occurs here. Model identity probes are GET-only and lazy:
native pages do not depend on a VL/embedding server. A runner holds one flock
through extraction, embeddings and the final KB transaction.
"""
from __future__ import annotations

import base64
import contextlib
import contextvars
import dataclasses
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

SCHEMA = "codetrail.ingest_checkpoint/2"
LEGACY_SCHEMA = "codetrail.ingest_checkpoint/1"
# Storage v2 adds the journal. Keep the input identity stable so a validated v1
# snapshot can migrate without throwing away completed work. Older writers
# reject the v2 storage marker instead of ignoring its unmerged journal units.
IDENTITY_SCHEMA = LEGACY_SCHEMA
UNIT_SCHEMA = "codetrail.ingest_unit/1"
JOURNAL_SCHEMA = "codetrail.ingest_journal/1"
MAX_BYTES = 128 * 1024 * 1024
MAX_JOURNAL_BYTES = 4 * MAX_BYTES
SOURCE_MAX_BYTES = 1024 * 1024 * 1024
MAX_UNITS = 200000
_ACTIVE = contextvars.ContextVar("codetrail_ingest_checkpoint", default=None)
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_FIGURE = re.compile(r"fig_[0-9a-f]{16}\Z")
_STATES = frozenset({"pending", "running", "succeeded", "failed", "missing", "needs_review"})


class CheckpointError(RuntimeError):
    """Invalid, unsafe, stale or concurrently owned checkpoint; never a cache miss."""


def current_job():
    return _ACTIVE.get()


def json_safe(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CheckpointError("checkpoint contains a non-finite number")
        return value
    if isinstance(value, (tuple, list)):
        return [json_safe(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: json_safe(item) for key, item in value.items()}
    if type(value).__module__.split(".")[0] in {"fitz", "pymupdf"} and type(value).__name__ in {"Rect", "IRect", "Point", "Matrix"}:
        return [json_safe(item) for item in value]
    raise CheckpointError(f"unsupported checkpoint value: {type(value).__name__}")


def _bytes(value):
    return json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CheckpointError("duplicate JSON key in checkpoint")
        result[key] = value
    return result


def _decode(data):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(CheckpointError("non-finite checkpoint JSON")))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise CheckpointError(f"invalid checkpoint JSON: {exc}") from exc


@dataclasses.dataclass(frozen=True)
class ResumeOptions:
    resume: bool = True
    redo_pages: tuple[int, ...] = ()
    redo_figures: tuple[str, ...] = ()
    retry_failed: bool = False

    @classmethod
    def validate(cls, *, resume=True, redo_pages=None, redo_figures=None,
                 retry_failed=False, fresh=False, preflight_only=False):
        if any(type(item) is not bool for item in (resume, retry_failed, fresh, preflight_only)):
            raise CheckpointError("resume/retry_failed/fresh/preflight_only require JSON booleans")
        pages = redo_pages if redo_pages is not None else ()
        figures = redo_figures if redo_figures is not None else ()
        if not isinstance(pages, (list, tuple)) or any(type(p) is not int or p < 1 for p in pages):
            raise CheckpointError("redo_pages requires positive, one-based integer pages")
        if not isinstance(figures, (list, tuple)) or any(not isinstance(f, str) or not _FIGURE.fullmatch(f) for f in figures):
            raise CheckpointError("redo_figures requires exact figure IDs")
        if len(set(pages)) != len(pages) or len(set(figures)) != len(figures):
            raise CheckpointError("redo selectors must not contain duplicates")
        if len(pages) + len(figures) > 100000:
            raise CheckpointError("too many redo selectors")
        if sum((bool(pages), bool(figures), retry_failed)) > 1:
            raise CheckpointError("redo_pages, redo_figures and retry_failed are mutually exclusive")
        selected = bool(pages or figures or retry_failed)
        if selected and (fresh or preflight_only or not resume):
            raise CheckpointError("selective redo requires resume and cannot be combined with fresh/preflight_only")
        return cls(resume, tuple(pages), tuple(figures), retry_failed)

    @property
    def selective(self):
        return bool(self.redo_pages or self.redo_figures or self.retry_failed)


def parse_pages(text):
    result = []
    for part in text.split(","):
        if not re.fullmatch(r"[1-9][0-9]*(?:-[1-9][0-9]*)?", part):
            raise CheckpointError("pages must look like 1,3-5 (one-based)")
        ends = [int(value) for value in part.split("-")]
        first, last = ends[0], ends[-1]
        if last < first or last - first > 100000 or last > 1000000:
            raise CheckpointError("invalid or excessive page range")
        result.extend(range(first, last + 1))
    if not result or len(result) != len(set(result)):
        raise CheckpointError("empty or duplicate page selection")
    return result


def _capabilities():
    if not hasattr(os, "geteuid") or not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0):
        raise CheckpointError("checkpoint requires POSIX owner, nofollow and directory-fd support")
    for name in ("open", "mkdir", "stat", "rename", "unlink"):
        if getattr(os, name) not in os.supports_dir_fd:
            raise CheckpointError(f"checkpoint requires dir_fd for {name}")


def _regular(fd, *, private=True):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise CheckpointError("checkpoint/source is not a regular file")
    if private and (info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077):
        raise CheckpointError("checkpoint must be owner-only, single-link and owned by the current user")
    return info


def _step(fd, name, *, create, private):
    if name in ("", ".", "..") or "/" in name or "\\" in name:
        raise CheckpointError("invalid checkpoint directory component")
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=fd)
        except FileExistsError:
            pass
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        info = os.fstat(child)
        # .codetrail is shared with lessons and may already be 0775 under
        # umask 002. Match figure_review there; ingest/doc stay owner-only.
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & (0o077 if private else 0o002):
            raise CheckpointError("unsafe owner or permissions on checkpoint directory")
        return child
    except BaseException:
        os.close(child)
        raise


@contextlib.contextmanager
def _directory(root, parts, *, create):
    _capabilities()
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(parts):
            child = _step(fd, part, create=create, private=index > 0)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _read(fd, name):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        info = _regular(handle)
        if info.st_size > MAX_BYTES:
            raise CheckpointError("checkpoint exceeds bounded read limit")
        result = bytearray()
        while True:
            block = os.read(handle, min(65536, MAX_BYTES + 1 - len(result)))
            if not block:
                break
            result.extend(block)
            if len(result) > MAX_BYTES:
                raise CheckpointError("checkpoint grew beyond bounded read limit")
        after = os.fstat(handle)
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise CheckpointError("checkpoint changed during read")
        return bytes(result)
    finally:
        os.close(handle)


def _atomic(fd, name, data):
    if len(data) > MAX_BYTES:
        raise CheckpointError("checkpoint exceeds its own bounded reader")
    try:
        old = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        pass
    else:
        try:
            _regular(old)
        finally:
            os.close(old)
    temporary = ".tmp-" + uuid.uuid4().hex
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        os.fchmod(handle, 0o600)
        remaining = memoryview(data)
        while remaining:
            count = os.write(handle, remaining)
            if count <= 0:
                raise CheckpointError("short checkpoint write")
            remaining = remaining[count:]
        os.fsync(handle)
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(handle)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=fd)


def source_identity(path, root):
    """Hash source bytes through anchored, nofollow descriptors, not mtime keys."""
    _capabilities()
    root = Path(root).resolve()
    full = Path(path).resolve()
    try:
        parts = full.relative_to(root).parts
    except ValueError as exc:
        raise CheckpointError("ingestion source must be inside the checkpoint project root") from exc
    if not parts:
        raise CheckpointError("ingestion source must be a file")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        handle = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = _regular(handle, private=False)
            if before.st_size > SOURCE_MAX_BYTES:
                raise CheckpointError("ingestion source exceeds checkpoint hashing limit (1 GiB)")
            hasher = hashlib.sha256()
            consumed = 0
            while True:
                block = os.read(handle, 1024 * 1024)
                if not block:
                    break
                consumed += len(block)
                if consumed > SOURCE_MAX_BYTES:
                    raise CheckpointError("source grew beyond checkpoint hashing limit")
                hasher.update(block)
            after = os.fstat(handle)
            named = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            fields = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if fields(before) != fields(after) or fields(after) != fields(named):
                raise CheckpointError("source changed while its digest was captured")
            return {"path": Path(*parts).as_posix(), "sha256": hasher.hexdigest(), "size": after.st_size}
        finally:
            os.close(handle)
    finally:
        os.close(fd)


def extraction_configuration():
    """Code/schema changes and every extraction tuning knob invalidate units."""
    import config
    names = ("RAG.py", "figure_extract.py", "figure_verify.py", "figure_candidates.py",
             "figure_quality.py", "document_structure.py", "extracted_document.py", "mineru_lane.py",
             "context_generation.py", "context_signals.py", "text_review.py", "elf_analysis.py")
    directory = Path(__file__).parent
    code = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in names}
    settings = {}
    for name in dir(config):
        if name.startswith(("FIGURE_", "PDF_", "VL_", "CHUNK_", "BIN_ELF_", "MINERU_", "KB_CONTEXT_")):
            value = getattr(config, name)
            if isinstance(value, (str, int, float, bool, list, tuple, dict)) or value is None:
                settings[name] = json_safe(value)
    import importlib.metadata
    versions = {}
    for package in ("pymupdf4llm", "PyMuPDF", "pyelftools"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "unavailable"
    return {"code": code, "settings": settings, "parsers": versions}


class IngestJob:
    def __init__(self, root, source, *, options=None, configuration=None, artifact_sha256=""):
        self.root = Path(root).resolve()
        import media
        sandbox = media.get_sandbox_root()
        if sandbox is not None and Path(sandbox).resolve() != self.root:
            raise CheckpointError("checkpoint root differs from the active sandbox")
        self.source_reference = Path(source).absolute()
        self.source_path = self.source_reference.resolve()
        self.source = source_identity(source, self.root)
        self._default_configuration = configuration is None
        self.options = options or ResumeOptions()
        self.identity = {"schema": IDENTITY_SCHEMA, "source": self.source,
                         "configuration": configuration if configuration is not None else extraction_configuration(),
                         "artifact_sha256": artifact_sha256}
        self.fingerprint = digest(self.identity)
        # Stable location identity lets a changed source explicitly invalidate
        # its old job instead of silently accumulating unreachable generations.
        self.slug = "doc-" + digest(self.source["path"])
        self.parts = (".codetrail", "ingest", self.slug)
        self.location = "/".join(self.parts) + "/state.json"
        self.state = {}
        self.fd = self.lock = None
        self.models = {}
        self.reused = set()
        self.redone = set()
        self.force_figures = set(self.options.redo_figures)
        self.force_pages = set(self.options.redo_pages)
        self._written_digest = None
        self._journal_fd = None
        self._journal_head = None

    def __enter__(self):
        if current_job() is not None:
            raise CheckpointError("nested ingestion checkpoint runner")
        self._directory = _directory(self.root, self.parts, create=not self.options.selective)
        try:
            self.fd = self._directory.__enter__()
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            if not self.options.selective:
                flags |= os.O_CREAT
            self.lock = os.open("runner.lock", flags, 0o600, dir_fd=self.fd)
            _regular(self.lock)
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CheckpointError("another ingestion runner owns this document checkpoint") from exc
            prior_head = None
            try:
                raw = _read(self.fd, "state.json")
            except FileNotFoundError:
                if self.options.selective:
                    raise CheckpointError("selective redo requires an existing checkpoint")
                if any(name != "runner.lock" for name in os.listdir(self.fd)):
                    raise CheckpointError("missing checkpoint state beside existing checkpoint artifacts")
                prior = None
            else:
                prior = _decode(raw)
                self._validate_state(prior)
                self._written_digest = hashlib.sha256(raw).hexdigest()
                prior, prior_head = self._replay_journal(self.fd, prior)
            if self.options.selective:
                if prior["fingerprint"] != self.fingerprint:
                    raise CheckpointError("source/configuration changed; selective redo cannot retain stale units; run a full ingest")
                known = prior.get("candidates", {})
                if set(self.options.redo_figures) - set(known):
                    raise CheckpointError("redo_figures contains unknown figure IDs")
                if any(page > prior.get("page_count", 0) for page in self.options.redo_pages):
                    raise CheckpointError("redo_pages exceeds the source page count")
            valid = prior is not None and prior["fingerprint"] == self.fingerprint and self.options.resume
            self.state = prior if valid else {"schema": SCHEMA, "fingerprint": self.fingerprint,
                "identity": self.identity, "units": {}, "models": {}, "candidates": {}, "page_count": 0,
                "status": "pending", "invalidation": "source/configuration changed" if prior else ""}
            if prior is not None and not valid:
                print("[ingest] checkpoint invalidated: " + (self.state["invalidation"] or "resume disabled"), flush=True)
            self.state["status"] = "running"
            self.state.pop("error", None)
            if valid and prior_head is not None:
                self._journal_head = prior_head
                log_name, _head_name = self._journal_names(prior_head)
                self._journal_fd = os.open(
                    log_name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
                info = _regular(self._journal_fd)
                if info.st_size < prior_head["offset"]:
                    raise CheckpointError("checkpoint journal is shorter than its committed head")
                if info.st_size > prior_head["offset"]:
                    # Bytes beyond the durable head were never published. All
                    # acknowledged units lie before this offset and survive.
                    os.ftruncate(self._journal_fd, prior_head["offset"])
                    os.fsync(self._journal_fd)
            else:
                self._new_journal()
            self._flush()
            self._token = _ACTIVE.set(self)
            return self
        except BaseException:
            self._close()
            raise

    @staticmethod
    def _validate_unit(key, unit):
        if not isinstance(key, str) or not isinstance(unit, dict) or unit.get("state") not in _STATES:
            raise CheckpointError("invalid checkpoint unit state")
        for field in ("fingerprint", "payload_sha256"):
            if field in unit and (not isinstance(unit[field], str) or not _SHA.fullmatch(unit[field])):
                raise CheckpointError("invalid checkpoint unit digest")
        if unit["state"] in {"succeeded", "missing", "needs_review"} and not all(
                field in unit for field in ("fingerprint", "payload_sha256")):
            raise CheckpointError("completed checkpoint unit is missing its content identity")
        if unit["state"] == "failed" and "payload_sha256" not in unit and not isinstance(unit.get("error"), str):
            raise CheckpointError("failed checkpoint unit has no failure evidence")

    @staticmethod
    def _journal_names(head):
        prefix = "units-" + head["generation"]
        return prefix + ".jsonl", prefix + ".head.json"

    @staticmethod
    def _validate_head(head, fingerprint):
        fields = {"schema", "generation", "checkpoint", "sequence", "offset", "digest"}
        if (not isinstance(head, dict) or set(head) != fields
                or head.get("schema") != JOURNAL_SCHEMA or head.get("checkpoint") != fingerprint
                or not isinstance(head.get("generation"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", head["generation"])
                or type(head.get("sequence")) is not int or head["sequence"] < 0
                or type(head.get("offset")) is not int or not 0 <= head["offset"] <= MAX_JOURNAL_BYTES
                or not isinstance(head.get("digest"), str) or not _SHA.fullmatch(head["digest"])):
            raise CheckpointError("invalid checkpoint journal head")
        if not head["sequence"] and (head["offset"] or head["digest"] != digest({
                "generation": head["generation"], "checkpoint": fingerprint})):
            raise CheckpointError("invalid checkpoint journal origin")

    def _validate_state(self, value):
        if not isinstance(value, dict) or value.get("schema") not in {SCHEMA, LEGACY_SCHEMA} or not isinstance(value.get("identity"), dict):
            raise CheckpointError("unsupported or malformed checkpoint schema")
        if value.get("fingerprint") != digest(value["identity"]):
            raise CheckpointError("checkpoint identity digest mismatch")
        units = value.get("units")
        if not isinstance(units, dict) or len(units) > MAX_UNITS:
            raise CheckpointError("invalid checkpoint unit inventory")
        for key, unit in units.items():
            self._validate_unit(key, unit)
        if not isinstance(value.get("candidates"), dict) or type(value.get("page_count")) is not int or value["page_count"] < 0:
            raise CheckpointError("invalid checkpoint source inventory")
        if value["schema"] == SCHEMA:
            self._validate_head(value.get("journal"), value["fingerprint"])
        elif "journal" in value:
            raise CheckpointError("legacy checkpoint cannot contain an unversioned journal")

    def _new_journal(self):
        generation = uuid.uuid4().hex
        head = {"schema": JOURNAL_SCHEMA, "generation": generation,
                "checkpoint": self.fingerprint, "sequence": 0, "offset": 0,
                "digest": digest({"generation": generation, "checkpoint": self.fingerprint})}
        log_name, head_name = self._journal_names(head)
        self._journal_fd = os.open(log_name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                   0o600, dir_fd=self.fd)
        os.fchmod(self._journal_fd, 0o600)
        os.fsync(self._journal_fd)
        os.fsync(self.fd)
        _atomic(self.fd, head_name, _bytes(head))
        self._journal_head = head

    def _replay_journal(self, fd, value):
        """Replay only the published prefix; never infer success from a tail."""
        if value.get("journal_failure"):
            raise CheckpointError("checkpoint journal publication could not be rolled back safely")
        watermark = value.get("journal")
        if watermark is None:
            return value, None
        log_name, head_name = self._journal_names(watermark)
        try:
            head = _decode(_read(fd, head_name))
            self._validate_head(head, value["fingerprint"])
            if (head["generation"] != watermark["generation"]
                    or head["sequence"] < watermark["sequence"] or head["offset"] < watermark["offset"]):
                raise CheckpointError("checkpoint journal head precedes its snapshot")
            handle = os.open(log_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError as exc:
            raise CheckpointError("missing checkpoint journal or committed head") from exc
        try:
            info = _regular(handle)
            if info.st_size < head["offset"] or info.st_size > MAX_JOURNAL_BYTES:
                raise CheckpointError("checkpoint journal size disagrees with its committed head")
            os.lseek(handle, watermark["offset"], os.SEEK_SET)
            remaining = head["offset"] - watermark["offset"]
            pending = bytearray()
            sequence, previous = watermark["sequence"], watermark["digest"]
            while remaining:
                block = os.read(handle, min(65536, remaining))
                if not block:
                    raise CheckpointError("checkpoint journal ended before its committed head")
                remaining -= len(block)
                pending.extend(block)
                while (end := pending.find(b"\n")) >= 0:
                    if end + 1 > MAX_BYTES:
                        raise CheckpointError("checkpoint journal record exceeds its bounded reader")
                    envelope = _decode(bytes(pending[:end]))
                    del pending[:end + 1]
                    if not isinstance(envelope, dict) or set(envelope) != {"record", "sha256"}:
                        raise CheckpointError("invalid checkpoint journal record")
                    record = envelope["record"]
                    if (not isinstance(record, dict) or set(record) != {"sequence", "previous", "name", "unit"}
                            or type(record.get("sequence")) is not int or record["sequence"] != sequence + 1
                            or record.get("previous") != previous
                            or envelope["sha256"] != digest(record)):
                        raise CheckpointError("checkpoint journal checksum or sequence mismatch")
                    self._validate_unit(record["name"], record["unit"])
                    value["units"][record["name"]] = record["unit"]
                    if len(value["units"]) > MAX_UNITS:
                        raise CheckpointError("too many checkpoint units")
                    sequence, previous = record["sequence"], envelope["sha256"]
                if len(pending) > MAX_BYTES:
                    raise CheckpointError("checkpoint journal record exceeds its bounded reader")
            if pending or sequence != head["sequence"] or previous != head["digest"]:
                raise CheckpointError("checkpoint journal does not reach its committed head")
        finally:
            os.close(handle)
        return value, head

    def _store_unit(self, name, unit):
        """Publish an O(1)-sized unit delta after its payload is durable."""
        self._validate_unit(name, unit)
        if name not in self.state["units"] and len(self.state["units"]) >= MAX_UNITS:
            raise CheckpointError("too many checkpoint units")
        head = self._journal_head
        log_name, head_name = self._journal_names(head)
        info = _regular(self._journal_fd)
        named = os.stat(log_name, dir_fd=self.fd, follow_symlinks=False)
        if ((info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
                or info.st_size != head["offset"] or _decode(_read(self.fd, head_name)) != head):
            raise CheckpointError("checkpoint journal changed outside the active runner")
        record = {"sequence": head["sequence"] + 1, "previous": head["digest"],
                  "name": name, "unit": unit}
        checksum = digest(record)
        raw = _bytes({"record": record, "sha256": checksum}) + b"\n"
        if len(raw) > MAX_BYTES or head["offset"] + len(raw) > MAX_JOURNAL_BYTES:
            raise CheckpointError("checkpoint journal exceeds its bounded reader")
        os.lseek(self._journal_fd, head["offset"], os.SEEK_SET)
        remaining = memoryview(raw)
        while remaining:
            count = os.write(self._journal_fd, remaining)
            if count <= 0:
                raise CheckpointError("short checkpoint journal write")
            remaining = remaining[count:]
        os.fsync(self._journal_fd)
        published = {**head, "sequence": record["sequence"],
                     "offset": head["offset"] + len(raw), "digest": checksum}
        try:
            _atomic(self.fd, head_name, _bytes(published))
        except BaseException:
            # A directory fsync can fail after rename. Do not let that failed
            # call publish a success on resume merely because the new name is
            # visible now. Roll back the head; a failed rollback poisons the
            # snapshot instead of guessing which publication was durable.
            try:
                _atomic(self.fd, head_name, _bytes(head))
            except BaseException:
                self.state["journal_failure"] = "head publication and rollback failed"
            raise
        # This order is interruption-safe: a snapshot must never advance its
        # watermark beyond units already installed in memory. If interrupted
        # before these assignments, recovery still reads the published delta.
        self.state["units"][name] = unit
        self._journal_head = published
        if name.startswith("document/"):
            self._flush()

    def _flush(self):
        self.state["schema"] = SCHEMA
        self.state["journal"] = dict(self._journal_head)
        self._validate_state(self.state)
        self.state["progress"] = self.report()
        raw = _bytes(self.state)
        _atomic(self.fd, "state.json", raw)
        self._written_digest = hashlib.sha256(raw).hexdigest()

    def _close(self):
        if self._journal_fd is not None:
            os.close(self._journal_fd)
            self._journal_fd = None
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None
        if self.fd is not None:
            self._directory.__exit__(None, None, None)
            self.fd = None

    def __exit__(self, kind, error, tb):
        try:
            if kind is not None:
                self.state["status"] = "interrupted" if issubclass(kind, (KeyboardInterrupt, SystemExit)) else "failed"
                self.state["error"] = f"{kind.__name__}: {error}"[:2000]
                self._flush()
        finally:
            _ACTIVE.reset(self._token)
            self._close()

    def model_identity(self, role):
        if role not in self.models:
            from model_identity import capture_model_identity
            identity = capture_model_identity(role)
            if not isinstance(identity, dict) or not _SHA.fullmatch(str(identity.get("fingerprint", ""))):
                raise CheckpointError(f"no reliable actual model identity for {role}")
            old = self.state["models"].get(role)
            if old and old.get("fingerprint") != identity["fingerprint"]:
                if self.options.selective:
                    raise CheckpointError(f"actual {role} model changed; selective redo cannot reuse old results")
                print(f"[ingest] {role} model changed; its cached units are invalidated", flush=True)
            self.models[role] = identity
            self.state["models"][role] = identity
            self._flush()
        return self.models[role]

    def key(self, kind, inputs=None, *, role=None):
        return digest({"job": self.fingerprint, "kind": kind, "inputs": inputs,
                       "model": self.model_identity(role)["fingerprint"] if role else None})

    def get(self, name, fingerprint, *, force=False):
        unit = self.state["units"].get(name)
        if force:
            self.redone.add(name)
            return None
        if not unit or unit.get("fingerprint") != fingerprint or "payload_sha256" not in unit or unit["state"] in {"pending", "running"}:
            return None
        try:
            raw = _read(self.fd, unit["payload_sha256"] + ".json")
        except FileNotFoundError as exc:
            raise CheckpointError(f"missing checkpoint payload: {name}") from exc
        if hashlib.sha256(raw).hexdigest() != unit["payload_sha256"]:
            raise CheckpointError(f"corrupted checkpoint payload: {name}")
        envelope = _decode(raw)
        if not isinstance(envelope, dict) or envelope.get("schema") != UNIT_SCHEMA or envelope.get("name") != name or envelope.get("fingerprint") != fingerprint:
            raise CheckpointError(f"checkpoint payload identity mismatch: {name}")
        self.reused.add(name)
        return envelope["payload"]

    def start(self, name, fingerprint, *, page=0):
        self._store_unit(name, {"state": "running", "fingerprint": fingerprint, "page": page})

    def put(self, name, fingerprint, payload, *, state="succeeded", page=0, reasons=()):
        if state not in _STATES:
            raise CheckpointError("invalid checkpoint state")
        raw = _bytes({"schema": UNIT_SCHEMA, "name": name, "fingerprint": fingerprint, "payload": payload})
        checksum = hashlib.sha256(raw).hexdigest()
        _atomic(self.fd, checksum + ".json", raw)
        self._store_unit(name, {"state": state, "fingerprint": fingerprint,
            "payload_sha256": checksum, "page": page, "reasons": list(reasons)})

    def fail(self, name, fingerprint, error, *, page=0):
        self._store_unit(name, {"state": "failed", "fingerprint": fingerprint,
                              "page": page, "error": (str(error) or type(error).__name__)[:2000]})

    def pages(self, count):
        if type(count) is not int or count < 1 or count > 100000:
            raise CheckpointError("invalid source PDF page count")
        if any(p > count for p in self.force_pages):
            raise CheckpointError("redo_pages exceeds the current PDF page count")
        self.state["page_count"] = count
        for page in range(1, count + 1):
            self.state["units"].setdefault(f"page/{page}", {"state": "pending", "page": page})
        self._flush()

    def page_forced(self, page):
        unit = self.state["units"].get(f"page/{page}", {})
        return page in self.force_pages or (self.options.retry_failed and unit.get("state") in {"failed", "missing"})

    def prepare_figures(self, candidates):
        ids = {c.figure_id: c.page for c in candidates}
        if set(self.options.redo_figures) - set(ids):
            raise CheckpointError("redo_figures does not exist in the current candidate plan")
        selected = set(self.force_figures)
        for c in candidates:
            previous = self.state["units"].get("figure/" + c.figure_id, {})
            if c.page in self.force_pages or (self.options.retry_failed and previous.get("state") == "failed"):
                selected.add(c.figure_id)
        # Shared VL results reference their representative's real input bytes.
        # Re-extract the whole asset group when any member is explicitly redone.
        groups = {(c.asset_digest, c.kind) for c in candidates if c.figure_id in selected}
        self.force_figures.update(c.figure_id for c in candidates if (c.asset_digest, c.kind) in groups)
        self.state["candidates"] = ids
        for name in list(self.state["units"]):
            if name.startswith("figure/") and name.split("/", 1)[1] not in ids:
                del self.state["units"][name]
        for fid, page in ids.items():
            self.state["units"].setdefault("figure/" + fid, {"state": "pending", "page": page})
        self._flush()

    def figure_key(self, candidate):
        import figure_extract
        role = None if figure_extract.read_native_lane(candidate) else "vl"
        return self.key("figure", dataclasses.asdict(candidate), role=role)

    def restore_figure(self, candidate):
        key = self.figure_key(candidate)
        payload = self.get("figure/" + candidate.figure_id, key,
                           force=candidate.figure_id in self.force_figures)
        if payload is None:
            return None
        from figure_verify import FigureResult
        from figure_candidates import Variant
        import figure_extract
        try:
            result = FigureResult(**payload["result"])
            if result.figure_id != candidate.figure_id or result.document_id != candidate.document_id or result.page != candidate.page:
                raise CheckpointError("cached figure source identity mismatch")
            if result.payload is not None:
                figure_extract.validate_payload(result.payload, result.kind)
            variants = []
            for item in payload["variants"]:
                value = dict(item)
                value["png"] = base64.b64decode(value["png"], validate=True)
                if hashlib.sha256(value["png"]).hexdigest() != value["digest"]:
                    raise CheckpointError("cached model input bytes do not match their digest")
                variant = Variant(**value)
                figure_extract.validate_variant(variant, where="checkpoint variant")
                variants.append(variant)
            return result, variants
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckpointError(f"invalid cached figure: {exc}") from exc

    def save_figure(self, candidate, result, variants):
        values = []
        for variant in variants:
            item = dataclasses.asdict(variant)
            if hashlib.sha256(item["png"]).hexdigest() != item["digest"]:
                raise CheckpointError("model input bytes changed before checkpoint")
            item["png"] = base64.b64encode(item["png"]).decode("ascii")
            values.append(item)
        status = ("failed" if result.extraction_status == "failed" else
                  "missing" if result.extraction_status == "skipped" else
                  "needs_review" if result.verification_status in {"needs_review", "unverified", "legacy_unverified"} else "succeeded")
        self.put("figure/" + candidate.figure_id, self.figure_key(candidate),
                 {"result": dataclasses.asdict(result), "variants": values},
                 state=status, page=result.page, reasons=result.reasons)

    def record_revision_baseline(self, figures, text):
        self.state["revision_provenance"] = {"figures": figures, "text": text}
        self._flush()

    def assert_valid(self):
        if self.source_reference.resolve() != self.source_path:
            raise CheckpointError("source alias changed before ingestion commit")
        if source_identity(self.source_path, self.root) != self.source:
            raise CheckpointError("source changed before ingestion commit")
        if self._default_configuration and self.identity["configuration"] != extraction_configuration():
            raise CheckpointError("extraction code/configuration changed during ingestion")
        with _directory(self.root, self.parts, create=False) as fd:
            if os.fstat(fd).st_ino != os.fstat(self.fd).st_ino or os.fstat(fd).st_dev != os.fstat(self.fd).st_dev:
                raise CheckpointError("checkpoint directory was replaced during ingestion")
            raw = _read(fd, "state.json")
            if hashlib.sha256(raw).hexdigest() != self._written_digest:
                raise CheckpointError("checkpoint state changed outside the active runner")
            snapshot, head = self._replay_journal(fd, _decode(raw))
            if head != self._journal_head or snapshot["units"] != self.state["units"]:
                raise CheckpointError("checkpoint journal changed outside the active runner")
            log_name, _ = self._journal_names(head)
            named = os.stat(log_name, dir_fd=fd, follow_symlinks=False)
            opened = _regular(self._journal_fd)
            if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                raise CheckpointError("checkpoint journal was replaced during ingestion")

    def validate_models(self):
        """Called outside the KB lock after inference, before publication."""
        from model_identity import capture_model_identity
        for role, expected in self.models.items():
            if capture_model_identity(role).get("fingerprint") != expected["fingerprint"]:
                raise CheckpointError(f"actual {role} model changed during ingestion")

    def complete(self, *, summary=None):
        self.assert_valid()
        self.state["status"] = "succeeded"
        self.state["report"] = self.report()
        if summary is not None:
            self.state["completion_summary"] = summary
        self._flush()

    def report(self):
        groups = {}
        for prefix in ("page/", "figure/"):
            units = {name: item for name, item in self.state["units"].items() if name.startswith(prefix)}
            groups[prefix[:-1]] = {"total": len(units),
                "counts": {state: sum(u["state"] == state for u in units.values()) for state in sorted(_STATES)},
                "reused": sum(name in self.reused for name in units),
                "redone": sum(name in self.redone for name in units),
                "issues": [{"id": name[len(prefix):], "page": unit.get("page", 0),
                            "state": unit["state"], "reasons": unit.get("reasons", []),
                            "error": unit.get("error", "")} for name, unit in units.items()
                           if unit["state"] in {"failed", "missing", "needs_review", "running"}]}
        return {"schema": SCHEMA, "checkpoint": self.location, "source": self.source,
                "status": self.state["status"], "source_pages": self.state["page_count"],
                "text_review": {"state": self.state["units"].get("document/text_review", {}).get("state", "not_applicable"),
                                "pending_text_ids": self.state["units"].get("document/text_review", {}).get("reasons", [])},
                "stages": {name.split("/", 1)[1]: unit["state"]
                           for name, unit in self.state["units"].items() if name.startswith("document/")}, **groups}

    def format_report(self):
        report = self.report()
        lines = [f"[ingest] checkpoint: {self.location}",
                 f"[ingest] document={report['status']} " +
                 " ".join(f"{stage}={state}" for stage, state in report["stages"].items())]
        for kind in ("page", "figure"):
            group = report[kind]
            lines.append(f"[ingest] {kind}: total={group['total']} reused={group['reused']} redone={group['redone']} "
                         + " ".join(f"{key}={value}" for key, value in group["counts"].items() if value))
            for issue in group["issues"][:40]:
                lines.append(f"  {kind}={issue['id']} p{issue['page']} {issue['state']}: "
                             + ",".join(issue["reasons"]) + issue["error"])
            if len(group["issues"]) > 40:
                lines.append(f"  ... {len(group['issues']) - 40} more; full inventory is in the checkpoint state")
        return "\n".join(lines)


def read_status(root, source):
    """Read-only progress snapshot; never creates directories or probes models."""
    job = IngestJob(root, source, configuration={})
    with _directory(job.root, job.parts, create=False) as fd:
        value = _decode(_read(fd, "state.json"))
        job._validate_state(value)
        value, _head = job._replay_journal(fd, value)
    job.state = value
    progress = job.report()
    progress["source"] = value["identity"]["source"]
    for group in ("page", "figure"):
        for metric in ("reused", "redone"):
            progress[group][metric] = value.get("progress", {}).get(group, {}).get(metric, 0)
    return {"status": value["status"], "progress": progress,
            "error": value.get("error", ""), "checkpoint": job.location,
            "source_matches": value["identity"].get("source") == job.source,
            "completion_summary": value.get("completion_summary", "")}
