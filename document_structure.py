"""Conservative PDF text roles and spans, without changing source coordinates.

The classifier accepts one page or a complete candidate region. A role describes
the whole input; mixed pages remain content and expose only their proven spans.
Geometry and a generic word such as ``directory`` are not role evidence. This
module never reconstructs indentation from PDF word boxes.
"""
from __future__ import annotations

import re
from collections.abc import Callable


_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)$")
_TREE_BRANCH = re.compile(r"^[ \t│┃|]*(?:[├└╰╞╘+`\\][─━-]{1,3}|\|--)[ \t]*\S")
_TREE_TRUNK = re.compile(r"^[ \t│┃|]+$")
_TREE_ROOT = re.compile(r"^(?:\.{1,2}|[^\s|]+[/\\])$")
_TABLE_RULE = re.compile(r"^[ \t|:-]+$")
_CODE_START = re.compile(
    r"^(?:"
    r"(?:if|for|while|switch|catch)\s*\(.*\)\s*[{;]"
    r"|(?:def|async\s+def)\s+[A-Za-z_]\w*\s*\(.*\)\s*(?:->[^:]+)?:"
    r"|class\s+[A-Za-z_]\w*(?:\([^)]*\))?\s*:"
    r"|(?:if|elif|for|while|with|except)\s+.+:"
    r"|(?:else|try|finally)\s*:"
    r"|(?:return|break|continue|throw)\b.*;"
    r"|\#\s*(?:include\s*[<\"]|define\s+\w+|(?:ifdef|ifndef|undef)\s+\w+|endif\b)"
    r"|(?:(?:static|const|unsigned|signed|volatile|extern|inline)\s+)*"
    r"(?:int|void|char|short|long|float|double|bool|size_t|u?int\d+_t)\s+.+[;{=]"
    r"|(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*="
    r"|(?:from\s+[\w.]+\s+import\s+|import\s+[\w.]+(?:\s+as\s+\w+)?\s*$)"
    r"|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*|->\w+|\[[^\]]+\])*\s*"
    r"(?:[+*&|/-]?=|<<=|>>=)(?!=).+;"
    r"|[A-Za-z_]\w*(?:\.\w+|->\w+)*\([^\n]*\)\s*;?\s*$"
    r")"
)
_CODE_CONTINUATION = re.compile(r"^(?:[{}\[\]();,]+|}\s*else\s*{?|//.*|/\*.*|\*/.*)$")
_NAV_TITLE = re.compile(
    r"^(?:#{1,6}\s+)?(?:table\s+of\s+contents|contents|"
    r"(?:list|table)\s+of\s+(?:figures|tables|illustrations)|"
    r"(?:章節|章节|圖表|图表|圖|图|表)?目[錄录])"
    r"(?:\s*[:：])?(?:\s*[（(]?(?:continued|cont\.?|續|续)[）)]?)?$", re.IGNORECASE)
_PAGE_REF = r"(?:[A-Z]-)?(?:\d{1,5}|[ivxlcdm]{1,8})(?:[-–—](?:\d{1,5}|[ivxlcdm]{1,8}))?"
_PAGE_REF_ONLY = re.compile(rf"^{_PAGE_REF}$", re.IGNORECASE)
_NAV_TAIL = re.compile(
    rf"^(.*?)(\s+|:[ \t]*|(?<=[.…·•_]))(\b{_PAGE_REF})[ \t]*$", re.IGNORECASE)
_LEADER = re.compile(r"(?:\.{2,}|…+|[·•_]{3,}|(?:\. ){3,})[ \t]*$")
_NAV_HEADER = re.compile(
    r"^(?:title|section|chapter|figure|table|description|標題|标题|章節|章节|項目|项目)"
    r"\s*(?:\||:)\s*(?:page(?:\s*(?:no\.?|number))?|頁(?:碼)?|页(?:码)?)$",
    re.IGNORECASE)
_NAV_LABEL = re.compile(r"^(?:\d+(?:\.\d+)*[.)]?\s+|(?:figure|table|圖|图|表)\s*\d)",
                        re.IGNORECASE)


def _lines(text: str) -> list[tuple[int, int, str]]:
    result = []
    offset = 0
    for line in text.splitlines(keepends=True):
        end = offset + len(line)
        result.append((offset, end, line.rstrip("\r\n")))
        offset = end
    return result


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _table_line_indices(lines) -> set[int]:
    """A real table cell containing code is not a code region."""
    result = set()
    # A literal || operator is not a cell boundary. Scan a run once, so a PDF
    # with repeated table separators does not cause quadratic rescanning.
    run = []
    has_rule = False
    for i, (_start, _end, line) in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2:
            result.add(i)
        if re.search(r"(?<!\|)\|(?!\|)", stripped) and not _TREE_BRANCH.match(stripped):
            run.append(i)
            has_rule = has_rule or ("-" in stripped and bool(_TABLE_RULE.fullmatch(stripped)))
        else:
            if has_rule:
                result.update(run)
            run = []
            has_rule = False
    if has_rule:
        result.update(run)
    return result


def _protected_regions(text: str) -> list[tuple[int, int, str]]:
    lines = _lines(text)
    table_lines = _table_line_indices(lines)
    occupied = set()
    regions = []
    i = 0
    while i < len(lines):
        match = _FENCE.match(lines[i][2])
        if match is None or i in table_lines:
            i += 1
            continue
        marker = match[1]
        end = i + 1
        while end < len(lines):
            close = _FENCE.match(lines[end][2])
            if close and close[1][0] == marker[0] and len(close[1]) >= len(marker) and not close[2].strip():
                end += 1
                break
            end += 1
        role = "file_tree" if sum(bool(_TREE_BRANCH.match(line[2]))
                                  for line in lines[i + 1:end]) >= 2 else "code"
        regions.append((lines[i][0], lines[end - 1][1], role))
        occupied.update(range(i, end))
        i = end

    i = 0
    while i < len(lines):
        if i in occupied or i in table_lines or not _TREE_BRANCH.match(lines[i][2]):
            i += 1
            continue
        end = i
        branches = 0
        last_branch = i
        while end < len(lines) and end not in occupied and end not in table_lines:
            line = lines[end][2]
            if _TREE_BRANCH.match(line):
                branches += 1
                last_branch = end
            elif line.strip() and not _TREE_TRUNK.fullmatch(line):
                break
            end += 1
        if branches >= 2:
            start = i
            if i and i - 1 not in occupied and _TREE_ROOT.fullmatch(lines[i - 1][2].strip()):
                start -= 1
            regions.append((lines[start][0], lines[last_branch][1], "file_tree"))
            occupied.update(range(start, last_branch + 1))
        i = max(i + 1, end)

    i = 0
    while i < len(lines):
        if i in occupied or i in table_lines or not _CODE_START.match(lines[i][2].strip()):
            i += 1
            continue
        end = i + 1
        last_code = i
        while end < len(lines) and end not in occupied and end not in table_lines:
            line = lines[end][2].strip()
            if (_CODE_START.match(line) or _CODE_CONTINUATION.fullmatch(line)
                    or (lines[end][2][:1].isspace() and line.startswith("#"))):
                last_code = end
            elif line:
                break
            end += 1
        regions.append((lines[i][0], lines[last_code][1], "code"))
        i = max(i + 1, end)
    return sorted(regions)


def protected_text_spans(text: str) -> list[tuple[int, int]]:
    """Half-open original offsets for code/fences/trees, including line endings."""
    return _merge([(start, end) for start, end, _role in _protected_regions(text)])


def normalize_preserving_structure(text: str, normalizer: Callable[[str], str]) -> str:
    """Normalize outside protected spans, preserving source bytes inside them.

    Interior boundary whitespace also stays intact: applying a strip-based
    normalizer independently must not join a fence or tree line with prose.
    With no protected region this is exactly the supplied normalizer.
    """
    spans = protected_text_spans(text)
    if not spans:
        return normalizer(text)
    parts = []
    offset = 0
    for start, end in spans + [(len(text), len(text))]:
        segment = text[offset:start]
        if segment:
            left = len(segment) - len(segment.lstrip())
            right = len(segment.rstrip())
            if left >= right:
                parts.append(segment)
            else:
                parts.append(segment[:left] + normalizer(segment[left:right]) + segment[right:])
        parts.append(text[start:end])
        offset = end
    return "".join(parts)


def _navigation_entry(line: str) -> tuple[bool, bool]:
    stripped = line.strip().strip("|").strip()
    if stripped.startswith("#"):
        return False, False
    cells = [cell.strip() for cell in stripped.split("|")]
    if len(cells) == 2 and _PAGE_REF_ONLY.fullmatch(cells[-1]):
        body, strong = cells[0], False
    else:
        match = _NAV_TAIL.fullmatch(stripped)
        if not match:
            return False, False
        body = match[1].rstrip()
        strong = bool(_LEADER.search(body))
        body = _LEADER.sub("", body).strip()
        separator = match[2]
        if (not strong and ":" not in separator and "\t" not in separator
                and len(separator) < 2 and not _NAV_LABEL.match(body)):
            # A trailing measurement in body prose is not a page reference.
            return False, False
    # References need a title with letters; isolated page numbers or dotted
    # numeric values do not prove a navigation entry.
    if not any(ch.isalpha() for ch in body):
        return False, False
    return True, strong


def navigation_spans(text: str) -> list[tuple[int, int]]:
    """Only confirmed title/reference runs or >=3 continued leader/reference rows.

    Page titles never carry state to the next page. Mixed pages expose individual
    runs; code and file trees take precedence over any navigation-looking text.
    """
    lines = _lines(text)
    protected = protected_text_spans(text)
    blocked = {i for i, (start, end, _line) in enumerate(lines)
               if any(a < end and start < b for a, b in protected)}
    spans = []
    covered = set()
    for i, (_start, _end, line) in enumerate(lines):
        if i in blocked or not _NAV_TITLE.fullmatch(line.strip()):
            continue
        cursor = i + 1
        references = strong = 0
        last_reference = None
        empty_run = 0
        while cursor < len(lines) and cursor not in blocked:
            stripped = lines[cursor][2].strip()
            if not stripped:
                empty_run += 1
                if empty_run > 1:
                    break
                cursor += 1
                continue
            empty_run = 0
            if _NAV_HEADER.fullmatch(stripped.strip("|").strip()) or (
                    "|" in stripped and "-" in stripped and _TABLE_RULE.fullmatch(stripped)):
                cursor += 1
                continue
            is_entry, has_leader = _navigation_entry(stripped)
            if not is_entry:
                break
            references += 1
            strong += has_leader
            last_reference = cursor
            cursor += 1
        if last_reference is not None and (references >= 2 or strong):
            spans.append((lines[i][0], lines[last_reference][1]))
            covered.update(range(i, last_reference + 1))

    i = 0
    while i < len(lines):
        if i in blocked or i in covered or not _navigation_entry(lines[i][2])[1]:
            i += 1
            continue
        end = i + 1
        while (end < len(lines) and end not in blocked and end not in covered
               and _navigation_entry(lines[end][2])[1]):
            end += 1
        if end - i >= 3:
            spans.append((lines[i][0], lines[end - 1][1]))
        i = end
    return _merge(spans)


def classify_text_role(text: str) -> str:
    """Return code/file_tree/navigation/content for a page or complete region."""
    lines = [(a, b, line) for a, b, line in _lines(text) if line.strip()]
    if not lines:
        return "content"
    regions = _protected_regions(text)
    for role in ("file_tree", "code"):
        count = sum(any(a <= start and end <= b and found == role
                        for a, b, found in regions) for start, end, _line in lines)
        if count and count / len(lines) >= 0.6:
            return role
    navigation = navigation_spans(text)
    if navigation and all(any(a <= start and end <= b for a, b in navigation)
                          or _PAGE_REF_ONLY.fullmatch(line.strip())
                          for start, end, line in lines):
        return "navigation"
    return "content"
