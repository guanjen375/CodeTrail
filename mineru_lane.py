"""Read a local MinerU flat content list as an explicit, unverified text lane.

The JSON and its generating PDF are one source identity.  Reading either file
never follows a symlink, creates a directory, launches MinerU, or reads an image
path from the JSON.  Native figure admission happens before ``build_document``;
this module changes text ownership and retrieval labels, never verification.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from document_structure import classify_text_role, navigation_spans
from extracted_document import ExtractedDocument, PAGE_SEPARATOR, Section, page_range_for_span


CONTENT_LIST_MAX_BYTES = 64 * 1024 * 1024
PDF_MAX_BYTES = 512 * 1024 * 1024
CONTENT_LIST_MAX_BLOCKS = 100_000
_NON_RETRIEVAL = frozenset({"header", "footer", "page_number"})
_TEXT_TYPES = frozenset({"text", "aside_text", "page_footnote", "code", "list",
                         "equation", "interline_equation"})
_SUPPORTED_TYPES = _TEXT_TYPES | _NON_RETRIEVAL | {"table", "image"}
_READABLE_KEYS = frozenset({"text", "table_body", "table_caption", "table_footnote",
                           "image_caption", "image_footnote", "code_body", "code_caption",
                           "list_items", "content", "children", "blocks"})
_TEXT_FIGURE_ORIGINS = frozenset({"figure_prose", "figure_terminal"})
_STRUCTURED_KINDS = frozenset({"table", "diagram"})
_EPSILON = 1e-6


class MineruError(ValueError):
    """An explicit MinerU request cannot be imported without losing provenance."""


# Public spelling used by ingestion/CLI validation as well as this converter.
MineruLaneError = MineruError


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


@dataclass(frozen=True)
class MineruPageGeometry:
    page: int
    display_rect: tuple[float, float, float, float]
    derotation: tuple[float, float, float, float, float, float]
    unrotated_rect: tuple[float, float, float, float]


@dataclass(frozen=True)
class MineruBlock:
    index: int
    page: int
    kind: str
    bbox: tuple[float, float, float, float]
    text_level: int
    markdown: str
    title: str = ""
    caption: str = ""
    footnote: str = ""
    # Original artifact markdown coordinates, before structured-owner markers.
    char_start: int = 0
    char_end: int = 0


@dataclass(frozen=True)
class MineruArtifact:
    root: Path
    content_list_path: Path
    pdf_path: Path
    pdf_sha256: str
    content_list_sha256: str
    pages: tuple[MineruPageGeometry, ...]
    blocks: tuple[MineruBlock, ...]
    page_markdown: tuple[tuple[int, str], ...]
    represented_pages: tuple[int, ...]
    missing_pages: tuple[int, ...]
    readable_page_count: int
    content_list_bytes: bytes = field(repr=False)
    _pdf_identity: _FileIdentity = field(repr=False)
    _content_list_identity: _FileIdentity = field(repr=False)

    @property
    def page_count(self) -> int:
        return len(self.pages)


def _absolute_root(root) -> Path:
    value = Path(root).expanduser()
    if ".." in value.parts:
        raise MineruError("MinerU sandbox root must not contain '..'")
    return value if value.is_absolute() else Path.cwd() / value


def _source_path(value, root: Path) -> Path:
    path = Path(value).expanduser()
    if ".." in path.parts:
        raise MineruError("MinerU source path must not contain '..'")
    path = path if path.is_absolute() else root / path
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise MineruError("MinerU source is outside the sandbox") from exc
    if not relative.parts:
        raise MineruError("MinerU source must be a regular file")
    return path


@contextmanager
def _open_source(path: Path, root: Path):
    """Walk the original lexical path with anchored directory descriptors."""
    path = _source_path(path, root)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = None
    file_fd = None
    try:
        # Starting at / also protects a symlink in the sandbox root's parents.
        fd = os.open(path.anchor, directory_flags)
        for component in path.parts[1:-1]:
            child = os.open(component, directory_flags, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise MineruError(f"MinerU source {path.name!r} is not a regular file")
        yield file_fd, fd
    except OSError as exc:
        raise MineruError(
            f"MinerU source {path.name!r} cannot be opened safely (missing or symlink path)") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if fd is not None:
            os.close(fd)


def _stat_key(info) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_source(path: Path, root: Path, limit: int) -> tuple[bytes, _FileIdentity]:
    with _open_source(path, root) as (fd, parent_fd):
        before = os.fstat(fd)
        if before.st_size > limit:
            raise MineruError(f"MinerU source {path.name!r} exceeds the {limit}-byte limit")
        parts = []
        size = 0
        while True:
            piece = os.read(fd, min(1024 * 1024, limit + 1 - size))
            if not piece:
                break
            parts.append(piece)
            size += len(piece)
            if size > limit:
                raise MineruError(f"MinerU source {path.name!r} exceeds the {limit}-byte limit")
        after = os.fstat(fd)
        named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (_stat_key(before) != _stat_key(after) or _stat_key(after) != _stat_key(named)
                or not stat.S_ISREG(named.st_mode) or size != before.st_size):
            raise MineruError(f"MinerU source {path.name!r} changed while being read")
    # Rewalk, rather than trust a parent directory that could have been renamed.
    with _open_source(path, root) as (fd, _parent_fd):
        if _stat_key(os.fstat(fd)) != _stat_key(after):
            raise MineruError(f"MinerU source {path.name!r} changed while being read")
    data = b"".join(parts)
    return data, _FileIdentity(*_stat_key(after), hashlib.sha256(data).hexdigest())


def _bbox(value, where: str, *, normalized: bool = False):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(isinstance(v, bool) or not isinstance(v, (float, int))
                   or not math.isfinite(v) for v in value)):
        raise MineruError(f"{where}: bbox must contain four finite numbers")
    box = tuple(float(v) for v in value)
    if box[0] >= box[2] or box[1] >= box[3]:
        raise MineruError(f"{where}: bbox is empty or reversed")
    if normalized and any(v < 0 or v > 1000 for v in box):
        raise MineruError(f"{where}: MinerU bbox must be within 0..1000")
    return box


def _transform(box, matrix):
    a, b, c, d, e, f = matrix
    points = [(a * x + c * y + e, b * x + d * y + f)
              for x in (box[0], box[2]) for y in (box[1], box[3])]
    return (min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points))


def _pdf_pages(data: bytes) -> tuple[MineruPageGeometry, ...]:
    try:
        import pymupdf

        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.needs_pass or document.page_count < 1:
                raise MineruError("MinerU source PDF is encrypted or has no pages")
            result = []
            for number in range(document.page_count):
                page = document[number]
                display = _bbox(tuple(page.rect), f"PDF page {number + 1}")
                matrix = tuple(float(v) for v in page.derotation_matrix)
                if len(matrix) != 6 or not all(math.isfinite(v) for v in matrix):
                    raise MineruError(f"PDF page {number + 1}: invalid derotation matrix")
                unrotated = _bbox(_transform(display, matrix), f"PDF page {number + 1}")
                result.append(MineruPageGeometry(number + 1, display, matrix, unrotated))
            return tuple(result)
    except MineruError:
        raise
    except Exception as exc:
        raise MineruError("MinerU source PDF geometry is unavailable; native fallback is not automatic") from exc


def _block_bbox(value, geometry: MineruPageGeometry, where: str):
    box = _bbox(value, where, normalized=True)
    x0, y0, x1, y1 = geometry.display_rect
    display = (x0 + (x1 - x0) * box[0] / 1000,
               y0 + (y1 - y0) * box[1] / 1000,
               x0 + (x1 - x0) * box[2] / 1000,
               y0 + (y1 - y0) * box[3] / 1000)
    return _bbox(_transform(display, geometry.derotation), where)


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MineruError(f"MinerU JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(_value):
    raise MineruError("MinerU JSON contains a non-finite number")


def _string(value, where: str) -> str:
    if not isinstance(value, str):
        raise MineruError(f"{where}: expected text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise MineruError(f"{where}: invalid Unicode text") from exc
    return value


def _lines(value, where: str) -> str:
    if isinstance(value, str):
        return _string(value, where)
    if not isinstance(value, list):
        raise MineruError(f"{where}: expected text or a flat list of text")
    return "\n".join(_string(item, where) for item in value)


def _body(item: dict, key: str, where: str) -> str:
    # Some flat code/equation producers use text; conflicting fields are unsafe.
    present = [name for name in dict.fromkeys((key, "text")) if name in item]
    if not present:
        raise MineruError(f"{where}: missing {key}")
    bodies = [_string(item[name], f"{where}.{name}") for name in present]
    if any(value != bodies[0] for value in bodies[1:]):
        raise MineruError(f"{where}: conflicting text fields")
    return bodies[0]


def _join_parts(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part)


def _parse_block(item: dict, index: int, pages) -> MineruBlock:
    where = f"MinerU block {index}"
    if (not isinstance(item, dict) or not isinstance(item.get("type"), str)
            or item["type"] not in _SUPPORTED_TYPES):
        raise MineruError(f"{where}: unsupported flat block type; readable content cannot be discarded")
    kind = item["type"]
    readable_keys = {
        "table": {"text", "table_body", "table_caption", "table_footnote"},
        "image": {"image_caption", "image_footnote"},
        "code": {"text", "code_body", "code_caption"},
        "list": {"list_items"},
    }.get(kind, {"text"})
    if any(key in item for key in _READABLE_KEYS - readable_keys):
        raise MineruError(f"{where}: unsupported readable fields for {kind}")
    number = item.get("page_idx")
    if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number < len(pages):
        raise MineruError(f"{where}: invalid 0-based page_idx")
    page = number + 1  # The only page conversion. Everything downstream is 1-based.
    box = _block_bbox(item.get("bbox"), pages[number], where)
    level = item.get("text_level", 0)
    if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 6:
        raise MineruError(f"{where}: text_level must be 0..6")
    if level and kind != "text":
        raise MineruError(f"{where}: text_level headings require a text block")

    caption = footnote = title = ""
    if kind == "table":
        body = _body(item, "table_body", where)
        if not body.strip():
            raise MineruError(f"{where}: empty table_body")
        caption = _lines(item.get("table_caption", []), f"{where}.table_caption")
        footnote = _lines(item.get("table_footnote", []), f"{where}.table_footnote")
        markdown = _join_parts(caption, body, footnote)
    elif kind == "image":
        caption = _lines(item.get("image_caption", []), f"{where}.image_caption")
        footnote = _lines(item.get("image_footnote", []), f"{where}.image_footnote")
        markdown = _join_parts(caption, footnote)
        # img_path is provenance only; it is deliberately never opened.
    elif kind == "list":
        markdown = _lines(item.get("list_items"), f"{where}.list_items")
    elif kind == "code":
        body = _body(item, "code_body", where)
        if not body.strip():
            raise MineruError(f"{where}: empty code_body")
        caption = _lines(item.get("code_caption", []), f"{where}.code_caption")
        # A wrapper protects arbitrary source code from downstream text parsers;
        # the source body, including CRLF, tabs, and trailing spaces, is unchanged.
        fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", body)), default=0))
        markdown = _join_parts(caption, fence + "\n" + body
                               + ("" if body.endswith("\n") else "\n") + fence)
    else:
        markdown = _body(item, "text", where)
    if level:
        if not markdown.strip() or "\n" in markdown or "\r" in markdown:
            raise MineruError(f"{where}: a heading must be one nonempty explicit line")
        title = markdown.strip()
        markdown = "#" * level + " " + markdown
    return MineruBlock(index, page, kind, box, level, markdown, title, caption, footnote)


def _assemble(blocks, replacements=None):
    """Return pages and block locators for exactly the rendered source string."""
    replacements = replacements or {}
    by_page = {}
    for block in blocks:
        by_page.setdefault(block.page, []).append(block)
    pages, spans, locators = [], [], {}
    document_offset = 0
    for page, page_blocks in by_page.items():
        parts = []
        offset = 0
        for block in page_blocks:
            value = replacements.get(block.index, block.markdown)
            if value and parts:
                offset += len(PAGE_SEPARATOR)
            start = offset
            if value:
                parts.append(value)
                offset += len(value)
            locators[block.index] = (document_offset + start, document_offset + offset)
        text = PAGE_SEPARATOR.join(parts)
        pages.append((page, text))
        spans.append((page, document_offset, document_offset + len(text)))
        document_offset += len(text) + len(PAGE_SEPARATOR)
    return tuple(pages), spans, locators


def load_artifact(content_list_path, pdf_path, expected_pdf_sha256, *, root) -> MineruArtifact:
    """Validate a flat local artifact against the PDF digest supplied by its caller."""
    if (not isinstance(expected_pdf_sha256, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_pdf_sha256)):
        raise MineruError("MinerU requires the generating PDF's 64-character SHA-256")
    root_path = _absolute_root(root)
    pdf = _source_path(pdf_path, root_path)
    content = _source_path(content_list_path, root_path)
    if pdf.suffix.lower() != ".pdf":
        raise MineruError("MinerU content lists apply only to one PDF")
    pdf_bytes, pdf_identity = _read_source(pdf, root_path, PDF_MAX_BYTES)
    if pdf_identity.sha256 != expected_pdf_sha256.lower():
        raise MineruError("MinerU generating PDF SHA-256 does not match the current source PDF")
    raw, json_identity = _read_source(content, root_path, CONTENT_LIST_MAX_BYTES)
    try:
        parsed = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_json_object,
                            parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise MineruError("MinerU content list is not valid UTF-8 flat JSON") from exc
    if not isinstance(parsed, list) or not parsed or len(parsed) > CONTENT_LIST_MAX_BLOCKS:
        raise MineruError("MinerU requires a nonempty legacy flat content list within the block limit")
    pages = _pdf_pages(pdf_bytes)
    blocks = tuple(_parse_block(item, index, pages) for index, item in enumerate(parsed))
    if any(left.page > right.page for left, right in zip(blocks, blocks[1:])):
        raise MineruError("MinerU flat blocks must preserve nondecreasing page reading order")
    markdown, _spans, locators = _assemble(blocks)
    blocks = tuple(replace(block, char_start=locators[block.index][0],
                           char_end=locators[block.index][1]) for block in blocks)
    readable_pages = {block.page for block in blocks
                      if block.kind not in _NON_RETRIEVAL and block.markdown.strip()}
    if not readable_pages:
        raise MineruError("MinerU content list contains no readable output")
    represented = tuple(page for page, _text in markdown)
    artifact = MineruArtifact(
        root_path, content, pdf, pdf_identity.sha256, json_identity.sha256, pages, blocks,
        markdown, represented, tuple(page for page in range(1, len(pages) + 1)
                                      if page not in represented), len(readable_pages), raw,
        pdf_identity, json_identity,
    )
    revalidate_artifact(artifact)
    return artifact


def revalidate_artifact(artifact: MineruArtifact) -> None:
    """Recheck both byte digests and named file identities before publication."""
    for path, expected, limit in (
            (artifact.pdf_path, artifact._pdf_identity, PDF_MAX_BYTES),
            (artifact.content_list_path, artifact._content_list_identity, CONTENT_LIST_MAX_BYTES)):
        _data, actual = _read_source(path, artifact.root, limit)
        if actual != expected:
            raise MineruError(f"MinerU source {path.name!r} changed after artifact validation")


@dataclass(frozen=True)
class _Owner:
    figure_id: str
    kind: str
    page: int
    bbox: tuple[float, float, float, float]
    rows: int


def _intersects(a, b) -> bool:
    return min(a[2], b[2]) - max(a[0], b[0]) > _EPSILON and min(a[3], b[3]) - max(a[1], b[1]) > _EPSILON


def _contains(outer, inner) -> bool:
    return (outer[0] <= inner[0] + _EPSILON and outer[1] <= inner[1] + _EPSILON
            and outer[2] + _EPSILON >= inner[2] and outer[3] + _EPSILON >= inner[3])


def _owners(chunks, artifact):
    owners = {}
    for chunk in chunks:
        kind = chunk.get("figure_kind")
        if kind not in _STRUCTURED_KINDS or chunk.get("auto_disposition") == "excluded":
            continue
        figure_id = chunk.get("figure_id")
        if not isinstance(figure_id, str) or not figure_id:
            raise MineruError("An admitted structured figure has no figure identity")
        if chunk.get("source", artifact.pdf_path.name) != artifact.pdf_path.name:
            raise MineruError("A structured owner belongs to another source PDF")
        occurrences = chunk.get("occurrences") or [{"page": chunk.get("page"), "bbox": chunk.get("bbox")}]
        if not isinstance(occurrences, (list, tuple)):
            raise MineruError(f"Structured owner {figure_id}: invalid occurrences")
        for occurrence in occurrences:
            if not isinstance(occurrence, dict):
                raise MineruError(f"Structured owner {figure_id}: invalid occurrence")
            page = occurrence.get("page")
            if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= artifact.page_count:
                raise MineruError(f"Structured owner {figure_id}: invalid 1-based page")
            box = _bbox(occurrence.get("bbox"), f"Structured owner {figure_id}")
            owner = _Owner(figure_id, kind, page, box, chunk.get("row_total") or 0)
            key = (figure_id, page, box)
            if key in owners and owners[key] != owner:
                raise MineruError(f"Structured owner {figure_id}: conflicting chunk metadata")
            owners[key] = owner  # Parts of the same canonical figure are one owner.
    return tuple(owners.values())


def _marker(owner: _Owner) -> str:
    if owner.kind == "table":
        from RAG import PDF_TABLE_REPLACED_MARKER

        return PDF_TABLE_REPLACED_MARKER.format(figure_id=owner.figure_id, page=owner.page, rows=owner.rows)
    return f"[圖表已改以結構化 chunk 收錄：figure={owner.figure_id} page={owner.page}]"


def _replacement(block, owners):
    if block.kind in _NON_RETRIEVAL or block.text_level:
        return None
    overlaps = [owner for owner in owners if owner.page == block.page and _intersects(owner.bbox, block.bbox)]
    if block.kind == "table":
        if (len(overlaps) != 1 or overlaps[0].kind != "table"
                or not (_contains(overlaps[0].bbox, block.bbox) or _contains(block.bbox, overlaps[0].bbox))):
            raise MineruError(f"MinerU table block {block.index} page {block.page}: no unique admitted structured table owner; use the native lane")
    elif not overlaps:
        return None
    elif block.kind == "code" or classify_text_role(block.markdown) in {"code", "file_tree"}:
        raise MineruError(f"MinerU code/tree block {block.index}: conflicting structured text owner")
    elif block.kind == "image":
        if len(overlaps) != 1 or not (_contains(overlaps[0].bbox, block.bbox) or _contains(block.bbox, overlaps[0].bbox)):
            raise MineruError(f"MinerU image block {block.index}: ambiguous structured geometry")
    elif len(overlaps) != 1 or not _contains(overlaps[0].bbox, block.bbox):
        # No source positions exist inside an OCR block. Cutting the entire
        # block here would also erase the prose outside the figure rectangle.
        raise MineruError(f"MinerU text block {block.index} page {block.page}: partial or ambiguous structured overlap")
    return _join_parts(block.caption, _marker(overlaps[0]), block.footnote)


def _merge_spans(spans):
    result = []
    for start, end in sorted(spans):
        if start >= end:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def _exclude_regions(start, end, excluded):
    cursor = start
    for a, b in excluded:
        if b <= cursor or a >= end:
            continue
        if cursor < a:
            yield cursor, a
        cursor = max(cursor, b)
        if cursor >= end:
            return
    if cursor < end:
        yield cursor, end


def _section_layout(raw, pages, blocks, locators, excluded):
    heads = [block for block in blocks if block.text_level
             and not any(a < locators[block.index][1] and locators[block.index][0] < b
                         for a, b in excluded)]
    sections, hierarchies, stack = [], [], []
    first = locators[heads[0].index][0] if heads else len(raw)
    if first:
        span = (0, first)
        sections.append(Section("", 0, span, page_range_for_span(span, pages)))
        hierarchies.append("")
    for position, block in enumerate(heads):
        start = locators[block.index][0]
        end = locators[heads[position + 1].index][0] if position + 1 < len(heads) else len(raw)
        while stack and stack[-1][0] >= block.text_level:
            stack.pop()
        stack.append((block.text_level, block.title))
        span = (start, end)
        sections.append(Section(block.title, block.text_level, span, page_range_for_span(span, pages)))
        hierarchies.append(" > ".join(title for _level, title in stack))
    return sections, hierarchies


def _source_chunks(text: str, split_chunks):
    """Use semantic source boundaries; never let a splitter rewrite OCR bytes."""
    if not text.strip():
        return
    parts = split_chunks(text, pre_normalized=True, include_heading=False,
                         overlap_chars=0, recognize_headings=False)
    if not parts:
        raise MineruError("MinerU readable source produced zero text chunks")
    starts = {0}
    for part in parts:
        start, end = part.get("char_start"), part.get("char_end")
        if (isinstance(start, bool) or not isinstance(start, int)
                or isinstance(end, bool) or not isinstance(end, int)
                or not 0 <= start <= end <= len(text)):
            raise MineruError("MinerU splitter returned invalid source coordinates")
        if start < len(text):
            starts.add(start)
    # Multiple sentence chunks can refer to one original long line. Keeping
    # that source line once is safer than duplicating or rewriting its symbols.
    boundaries = sorted(starts) + [len(text)]
    for start, end in zip(boundaries, boundaries[1:]):
        if text[start:end].strip():
            yield start, end, text[start:end]


def build_document(artifact: MineruArtifact, native_document: ExtractedDocument, *, split_chunks) -> ExtractedDocument:
    """Replace native text with MinerU text after native figure admission finishes."""
    if native_document.source and native_document.source != artifact.pdf_path.name:
        raise MineruError("MinerU and native document sources differ")
    native_figures = [chunk for chunk in native_document.chunks if chunk.get("structured") is True]
    replaced_figures = [chunk for chunk in native_figures if chunk.get("origin") in _TEXT_FIGURE_ORIGINS]
    retained_figures = [copy.deepcopy(chunk) for chunk in native_figures
                        if chunk.get("origin") not in _TEXT_FIGURE_ORIGINS]
    if any(chunk.get("figure_kind") not in _STRUCTURED_KINDS for chunk in retained_figures):
        raise MineruError("MinerU cannot determine a structured figure's text owner")
    if any(chunk.get("auto_disposition") == "excluded" for chunk in retained_figures):
        raise MineruError("MinerU cannot retain a quality-excluded structured owner")
    owners = _owners(retained_figures, artifact)
    owners_by_page, blocks_by_page = {}, {}
    for owner in owners:
        owners_by_page.setdefault(owner.page, []).append(owner)
    for block in artifact.blocks:
        blocks_by_page.setdefault(block.page, []).append(block)
    replacements = {}
    for block in artifact.blocks:
        replacement = _replacement(block, owners_by_page.get(block.page, ()))
        if replacement is not None:
            replacements[block.index] = replacement
    pages, page_spans, locators = _assemble(artifact.blocks, replacements)
    raw = PAGE_SEPARATOR.join(text for _page, text in pages)
    excluded = []
    for (_page, text), (_number, offset, _end) in zip(pages, page_spans):
        excluded.extend((offset + a, offset + b) for a, b in navigation_spans(text))
    excluded.extend(locators[block.index] for block in artifact.blocks if block.kind in _NON_RETRIEVAL)
    excluded = _merge_spans(excluded)
    sections, hierarchies = _section_layout(raw, page_spans, artifact.blocks, locators, excluded)
    document = ExtractedDocument(raw, sections, [], artifact.pdf_path.name,
                                 native_document.doc_type, page_spans, excluded)

    # A page title or a structured-owner marker is not evidence that MinerU
    # transcribed the prose/terminal text being removed on that page.
    text_pages = {block.page for block in artifact.blocks
                  if block.kind in _TEXT_TYPES and not block.text_level
                  and block.index not in replacements
                  and any(raw[a:b].strip() for a, b in _exclude_regions(*locators[block.index], excluded))}
    required_pages = {chunk.get("page") for chunk in replaced_figures}
    for chunk in replaced_figures:
        required_pages.update(occurrence.get("page") for occurrence in chunk.get("occurrences", [])
                              if isinstance(occurrence, dict))
    if required_pages - text_pages:
        raise MineruError(f"MinerU has no body text on pages {sorted(required_pages - text_pages)} whose native prose/terminal owner would be removed")

    counts = {}
    section_ends = [section.char_span[1] for section in sections]
    for page, page_start, page_end in page_spans:
        page_blocks = blocks_by_page[page]
        block_ends = [locators[block.index][1] for block in page_blocks]
        first_section = bisect_right(section_ends, page_start)
        for section_index in range(first_section, len(sections)):
            section = sections[section_index]
            if section.char_span[0] >= page_end:
                break
            start = max(page_start, section.char_span[0])
            end = min(page_end, section.char_span[1])
            if start >= end:
                continue
            for a, b in _exclude_regions(start, end, excluded):
                for local_start, local_end, body in _source_chunks(raw[a:b], split_chunks):
                    chunk_start, chunk_end = a + local_start, a + local_end
                    selected = []
                    first_block = bisect_right(block_ends, chunk_start)
                    for block_number in range(first_block, len(page_blocks)):
                        block = page_blocks[block_number]
                        if locators[block.index][0] >= chunk_end:
                            break
                        if locators[block.index][0] < locators[block.index][1]:
                            selected.append(block)
                    if not selected:
                        raise MineruError("MinerU chunk has no source block locator")
                    boxes = [block.bbox for block in selected]
                    hierarchy = hierarchies[section_index]
                    prefix = (f"[HEADING] {hierarchy}\n[SECTION] {section.title}\n" if section.title else "")
                    index = counts.get(page, 0)
                    counts[page] = index + 1
                    document.chunks.append({
                        "source": document.source, "page": page, "chunk_index": index,
                        "content": prefix + body, "type": document.doc_type,
                        "section": section.title, "heading_hierarchy": hierarchy,
                        "section_index": section_index,
                        "char_start": chunk_start, "char_end": chunk_end,
                        "heading_prefix_chars": len(prefix), "overlap_prefix_chars": 0,
                        "origin": "mineru_text", "text_lane": "mineru", "heading_source": "mineru",
                        "verification_status": "unverified",
                        "source_sha256": artifact.pdf_sha256,
                        "content_list_sha256": artifact.content_list_sha256,
                        "bbox": [min(box[0] for box in boxes), min(box[1] for box in boxes),
                                 max(box[2] for box in boxes), max(box[3] for box in boxes)],
                        "mineru_blocks": [{"index": block.index, "bbox": list(block.bbox),
                                           "source_char_start": block.char_start,
                                           "source_char_end": block.char_end} for block in selected],
                    })
                    import text_review
                    text_review.initialize_chunk(
                        document.chunks[-1],
                        source_path=artifact.pdf_path.relative_to(artifact.root).as_posix(),
                        artifact_path=artifact.content_list_path.relative_to(artifact.root).as_posix())

    for chunk in retained_figures:
        page = chunk.get("page")
        box = _bbox(chunk.get("bbox"), "Structured figure heading locator")
        matches = [block for block in blocks_by_page.get(page, ())
                   if block.kind not in _NON_RETRIEVAL and _intersects(box, block.bbox)
                   and not any(a < locators[block.index][1] and locators[block.index][0] < b for a, b in excluded)]
        section_index = -1
        caption = ""
        if len(matches) == 1:
            match = matches[0]
            section_index = document.section_index_for_offset(locators[match.index][0])
            caption = match.caption
        chunk.update({"section": sections[section_index].title if section_index >= 0 else "",
                      "heading_hierarchy": hierarchies[section_index] if section_index >= 0 else "",
                      "section_index": section_index, "figure_caption": caption,
                      "heading_source": "mineru", "source_sha256": artifact.pdf_sha256,
                      "content_list_sha256": artifact.content_list_sha256,
                      "chunk_index": counts.get(page, 0)})
        counts[page] = counts.get(page, 0) + 1
        document.chunks.append(chunk)
    if not document.chunks:
        raise MineruError("MinerU readable source produced no retrievable content")

    # Preserve every native admission/coverage guard without changing it. The
    # original document and replaced payloads remain available for local audit.
    for name, value in vars(native_document).items():
        if name not in native_document.__dataclass_fields__:
            setattr(document, name, value)
    document._codetrail_mineru_artifact = artifact
    document._codetrail_mineru_native_document = native_document
    document._codetrail_mineru_block_locators = tuple(
        {"index": block.index, "page": block.page, "bbox": list(block.bbox),
         "char_start": locators[block.index][0], "char_end": locators[block.index][1],
         "source_char_start": block.char_start, "source_char_end": block.char_end}
        for block in artifact.blocks)
    document._codetrail_mineru_summary = {
        "text_lane": "mineru", "source_sha256": artifact.pdf_sha256,
        "content_list_sha256": artifact.content_list_sha256,
        "page_count": artifact.page_count, "readable_page_count": artifact.readable_page_count,
        "represented_pages": list(artifact.represented_pages), "missing_pages": list(artifact.missing_pages),
        "text_pages": sorted(text_pages), "ocr_verification": "unverified",
        "replaced_figure_ids": sorted({chunk["figure_id"] for chunk in replaced_figures}),
        "replaced_figure_sources": [{"figure_id": chunk["figure_id"], "page": chunk.get("page"),
                                    "evidence_ref": chunk.get("evidence_ref", ""),
                                    "content_sha256": hashlib.sha256(chunk.get("content", "").encode("utf-8")).hexdigest()}
                                   for chunk in replaced_figures],
    }
    return document
