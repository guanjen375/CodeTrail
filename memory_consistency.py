"""Evidence-based ELF/linker/preload memory checks. No compiler or shell execution.

Addresses are half-open intervals with an explicit address space. The supported
grammars are deliberately finite; incomplete input can never produce a pass.
ELF bytes and every secondary input are read through the same anchored sandbox.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import shlex
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from runtime_dependencies import require_safe_filesystem

MAX_TEXT_BYTES = 16 * 1024 * 1024
MAX_ELF_BYTES = 512 * 1024 * 1024
MAX_RECORDS = 50000
MAX_ISSUES = 2000
MAX_ADDRESS = 1 << 64
_HEX = r"(?:0[xX])?[0-9a-fA-F]+"
_NUM = r"(?:0[xX][0-9a-fA-F]+|[0-9]+)"


class MemoryEvidenceError(ValueError):
    """An input is unsafe, inconsistent, or not a supported evidence record."""


@dataclass(frozen=True)
class Evidence:
    source: str
    sha256: str
    locator: str
    text: str = ""


@dataclass(frozen=True)
class Interval:
    name: str
    start: int
    size: int
    space: str
    kind: str
    evidence: Evidence
    alignment: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        for value in (self.start, self.size, self.alignment):
            if isinstance(value, bool) or not isinstance(value, int):
                raise MemoryEvidenceError("address, size and alignment must be integers")
        if self.start < 0 or self.start >= MAX_ADDRESS or self.size < 0:
            raise MemoryEvidenceError("negative or overflowing address/length")
        if self.end > MAX_ADDRESS:
            raise MemoryEvidenceError("address + length exceeds 64 bits")
        if self.alignment < 0 or (self.alignment and self.alignment & (self.alignment - 1)):
            raise MemoryEvidenceError("alignment must be zero or a power of two")
        if self.space not in ("vma", "lma", "file"):
            raise MemoryEvidenceError("address space must be vma, lma or file")

    @property
    def end(self):
        return self.start + self.size

    def public(self):
        return {"name": self.name, "start": self.start, "end": self.end,
                "length": self.size, "range": f"[0x{self.start:x}, 0x{self.end:x})",
                "space": self.space, "kind": self.kind, "alignment": self.alignment,
                "evidence": asdict(self.evidence)}


@dataclass
class Parsed:
    intervals: list[Interval] = field(default_factory=list)
    unknown: list[dict] = field(default_factory=list)
    format: str = ""
    constraints: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def uncertain(self, reason, evidence=None):
        if len(self.unknown) < MAX_ISSUES:
            self.unknown.append({"reason": reason,
                                 "evidence": asdict(evidence) if evidence else None})

    def add(self, interval):
        if len(self.intervals) >= MAX_RECORDS:
            raise MemoryEvidenceError("input exceeds the memory record limit")
        self.intervals.append(interval)


def _stat_key(s):
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def read_source(root, value, *, limit=MAX_TEXT_BYTES):
    """Read one stable, bounded regular file without following any symlink."""
    require_safe_filesystem("memory evidence read", error_type=MemoryEvidenceError)
    root = Path(root).absolute()
    p = Path(value)
    p = p if p.is_absolute() else root / p
    try:
        rel = p.relative_to(root)
    except ValueError as exc:
        raise MemoryEvidenceError("evidence source is outside the sandbox") from exc
    if not rel.parts or ".." in p.parts:
        raise MemoryEvidenceError("evidence path must be a sandbox file without '..'")
    directory = file_fd = None
    try:
        directory = os.open(p.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in p.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        file_fd = os.open(p.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=directory)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise MemoryEvidenceError("evidence must be a regular file without hard links")
        if before.st_size > limit:
            raise MemoryEvidenceError(f"evidence source exceeds {limit} bytes")
        pieces, count = [], 0
        while True:
            b = os.read(file_fd, min(1024 * 1024, limit + 1 - count))
            if not b:
                break
            count += len(b)
            if count > limit:
                raise MemoryEvidenceError("evidence grew beyond its size limit")
            pieces.append(b)
        after = os.fstat(file_fd)
        named = os.stat(p.name, dir_fd=directory, follow_symlinks=False)
        if _stat_key(before) != _stat_key(after) or _stat_key(after) != _stat_key(named):
            raise MemoryEvidenceError("evidence changed while being read")
        if count != before.st_size:
            raise MemoryEvidenceError("evidence was truncated while being read")
        data = b"".join(pieces)
        return data, Evidence(rel.as_posix(), hashlib.sha256(data).hexdigest(), "file")
    except OSError as exc:
        raise MemoryEvidenceError("evidence source missing, unsafe, or unreadable") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory is not None:
            os.close(directory)


def _line(source, number, text):
    return Evidence(source.source, source.sha256, f"line {number}", text[:1000])


def _integer(value):
    if isinstance(value, bool):
        raise MemoryEvidenceError("boolean is not a memory address")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(_NUM, value.strip()):
        s = value.strip()
        return int(s, 16 if s.lower().startswith("0x") else 10)
    raise MemoryEvidenceError("expected a non-negative integer or explicit 0x address")


def _json(text):
    def pairs(items):
        obj = {}
        for k, v in items:
            if k in obj:
                raise MemoryEvidenceError(f"duplicate JSON key: {k}")
            obj[k] = v
        return obj
    def bad_constant(_):
        raise MemoryEvidenceError("non-finite JSON constant")
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=bad_constant)
    except (ValueError, RecursionError) as exc:
        raise MemoryEvidenceError(f"invalid memory evidence JSON: {exc}") from exc


def parse_elf(data, source):
    # Share the mandatory backend and dependency failure policy with existing ELF views.
    import elf_analysis
    elf_analysis.require_pyelftools()
    from elftools.elf.elffile import ELFFile

    result = Parsed(format="elf")
    try:
        elf = ELFFile(io.BytesIO(data))
        bits = elf.elfclass
        result.metadata = {"machine": str(elf.header.e_machine), "bits": bits,
                           "type": str(elf.header.e_type), "source": asdict(source),
                           "section_names": []}
        if elf.header.e_type not in ("ET_EXEC", "ET_DYN"):
            result.uncertain("ELF is not a linked executable; final addresses are unknown", source)
        elif elf.header.e_type == "ET_DYN":
            result.uncertain("ET_DYN runtime load bias is not supplied; absolute runtime addresses are unknown", source)
        segments = []
        for i, seg in enumerate(elf.iter_segments()):
            if seg.header.p_type != "PT_LOAD":
                continue
            h = seg.header
            ev = Evidence(source.source, source.sha256, f"segment[{i}] PT_LOAD",
                          f"vaddr=0x{h.p_vaddr:x} paddr=0x{h.p_paddr:x} "
                          f"offset=0x{h.p_offset:x} filesz=0x{h.p_filesz:x} memsz=0x{h.p_memsz:x}")
            meta = {"filesz": h.p_filesz, "offset": h.p_offset, "vaddr": h.p_vaddr,
                    "paddr": h.p_paddr, "memsz": h.p_memsz}
            result.add(Interval(f"LOAD[{i}]", h.p_vaddr, h.p_memsz, "vma", "segment", ev,
                                h.p_align, meta))
            result.add(Interval(f"LOAD[{i}]", h.p_paddr, h.p_filesz, "lma", "load", ev,
                                1, meta))
            if h.p_memsz > h.p_filesz:
                result.add(Interval(f"LOAD[{i}] zero tail", h.p_vaddr + h.p_filesz,
                                    h.p_memsz - h.p_filesz, "vma", "zero", ev))
            segments.append((h, ev))
        for i, sec in enumerate(elf.iter_sections()):
            if i >= MAX_RECORDS:
                raise MemoryEvidenceError("ELF section inventory exceeds its record limit")
            result.metadata["section_names"].append(sec.name)
            h = sec.header
            if not h.sh_flags & 2:  # SHF_ALLOC only; debug sections are not runtime allocations.
                continue
            nobits = h.sh_type == "SHT_NOBITS"
            ev = Evidence(source.source, source.sha256, f"section[{i}] {sec.name}",
                          f"addr=0x{h.sh_addr:x} size=0x{h.sh_size:x} type={h.sh_type}")
            result.add(Interval(sec.name, h.sh_addr, h.sh_size, "vma", "section", ev,
                                h.sh_addralign, {"nobits": nobits, "offset": h.sh_offset,
                                                "flags": h.sh_flags, "elf_type": h.sh_type}))
            if nobits:
                result.add(Interval(sec.name, h.sh_addr, h.sh_size, "vma", "zero", ev))
                continue
            containing = [p for p, _ in segments
                          if p.p_vaddr <= h.sh_addr and h.sh_addr + h.sh_size <= p.p_vaddr + p.p_filesz
                          and p.p_offset <= h.sh_offset
                          and h.sh_offset + h.sh_size <= p.p_offset + p.p_filesz
                          and h.sh_addr - p.p_vaddr == h.sh_offset - p.p_offset]
            addresses = {p.p_paddr + h.sh_addr - p.p_vaddr for p in containing}
            if len(addresses) == 1:
                result.add(Interval(sec.name, next(iter(addresses)), h.sh_size, "lma", "section_load", ev))
            elif h.sh_size:
                result.uncertain(f"{sec.name}: no unique file-backed PT_LOAD mapping to LMA", ev)
        if not segments:
            result.uncertain("ELF has no PT_LOAD segments", source)
        if not result.metadata["section_names"]:
            result.uncertain("ELF has no section inventory; named output sections cannot be compared", source)
        result.metadata["byte_length"] = len(data)
        return result
    except MemoryEvidenceError:
        raise
    except Exception as exc:
        raise MemoryEvidenceError(f"cannot read ELF layout: {exc}") from exc


def parse_linker_map(text, source, *, format="auto"):
    if format not in ("auto", "gnu", "metaware"):
        raise MemoryEvidenceError("map_format must be auto, gnu or metaware")
    if format == "auto":
        if re.search(r"^\s*SECTIONS? SUMMARY\s*$", text, re.M):
            format = "metaware"
        elif "Linker script and memory map" in text:
            format = "gnu"
        else:
            out = Parsed(format="unknown")
            out.uncertain("linker map format is not recognized", source)
            return out
    out = Parsed(format=format)
    active = False
    pending = None
    header_seen = False
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        ev = _line(source, n, line)
        if format == "gnu":
            if "Linker script and memory map" in line:
                active = True
                continue
            if not active:
                continue
            # Output sections start in column 1; indented input sections/symbols do not.
            match = re.match(r"^(\S+)\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)(.*)$", line)
            if pending and re.match(r"^\s+0x", line):
                m = re.match(r"^\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)(.*)$", line)
                if m:
                    name, first_ev = pending
                    values = (name, m[1], m[2], m[3])
                    ev = Evidence(source.source, source.sha256,
                                  f"{first_ev.locator}; line {n}", first_ev.text + "\n" + line[:500])
                else:
                    out.uncertain("unparsed wrapped GNU output section", ev)
                    values = None
                pending = None
            elif match:
                values = match.groups()
                pending = None
            else:
                values = None
                if line and not line[0].isspace() and re.fullmatch(r"[A-Za-z_.$][\w.$-]*", stripped):
                    if stripped not in ("LOAD", "START", "END", "OUTPUT", "GROUP"):
                        pending = (stripped, ev)
                elif line and not line[0].isspace() and stripped.startswith("."):
                    out.uncertain("unparsed GNU output section row", ev)
            if values:
                name, start, length, tail = values
                out.add(Interval(name, int(start, 16), int(length, 16), "vma", "map_section", ev))
                lma = re.search(r"\bload address\s+(0x[0-9a-fA-F]+)", tail)
                if lma:
                    out.add(Interval(name, int(lma[1], 16), int(length, 16), "lma", "map_load", ev))
                else:
                    out.add(Interval(name, int(start, 16), int(length, 16), "lma", "map_load", ev))
            continue
        if re.fullmatch(r"SECTIONS? SUMMARY", stripped):
            active = True
            continue
        if not active:
            continue
        if re.match(r"[A-Z][A-Z ]+ SUMMARY", stripped):
            break
        if not stripped or set(stripped) <= {"-", "="}:
            continue
        if any(token in stripped for token in ("OUTPUT/", "INPUT SECTION", "START", "ADDRESS", "LENGTH")):
            header_seen = True
            continue
        m = re.fullmatch(r"(?:(\S+)\s+)?(text|data|bss|xdata)\s+(" + _HEX + r")\s+(" + _HEX + r")\s+(" + _HEX + r")", stripped, re.I)
        if m:
            name = m[1] or (pending[0] if pending else "")
            if not name:
                out.uncertain("MetaWare row has no section name", ev)
                continue
            if pending and not m[1]:
                ev = Evidence(source.source, source.sha256,
                              f"{pending[1].locator}; line {n}", pending[1].text + "\n" + line[:500])
            start, end, size = (int(m[k], 16) for k in (3, 4, 5))
            if end != start + size - 1:
                out.uncertain(f"MetaWare inclusive END disagrees with START/LENGTH for {name}", ev)
            out.add(Interval(name, start, size, "vma", "map_section", ev,
                             metadata={"type": m[2].lower()}))
            pending = None
        elif re.fullmatch(r"[A-Za-z_.$][\w.$-]*", stripped):
            if pending:
                out.uncertain("MetaWare section name has no address row", pending[1])
            pending = (stripped, ev)
        elif header_seen:
            out.uncertain("unparsed MetaWare SECTION SUMMARY row", ev)
    if pending:
        out.uncertain("section name has no parsed address/length", pending[1])
    if not out.intervals:
        out.uncertain("no output section records parsed from linker map", source)
    return out


def _expression(text, values):
    """A bounded numeric expression subset; unknown identifiers are never zero."""
    text = re.sub(r"\b(\d+)\s*([KMG])\b",
                  lambda m: str(int(m[1]) * 1024 ** ("KMG".index(m[2].upper()) + 1)), text.strip(), flags=re.I)
    if len(text) > 1024:
        raise MemoryEvidenceError("linker expression exceeds its limit")
    try:
        tree = ast.parse(text, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise MemoryEvidenceError("unsupported linker expression") from exc
    if sum(1 for _ in ast.walk(tree)) > 128:
        raise MemoryEvidenceError("linker expression is too complex")
    def visit(n):
        if isinstance(n, ast.Expression):
            value = visit(n.body)
        elif isinstance(n, ast.Constant) and type(n.value) is int:
            value = n.value
        elif isinstance(n, ast.Name) and n.id in values:
            value = values[n.id]
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            value = visit(n.operand) * (-1 if isinstance(n.op, ast.USub) else 1)
        elif isinstance(n, ast.BinOp):
            a, b = visit(n.left), visit(n.right)
            if isinstance(n.op, ast.Add): value = a + b
            elif isinstance(n.op, ast.Sub): value = a - b
            elif isinstance(n.op, ast.Mult): value = a * b
            elif isinstance(n.op, (ast.Div, ast.FloorDiv)) and b and a % b == 0: value = a // b
            elif isinstance(n.op, ast.LShift) and 0 <= b <= 63: value = a << b
            elif isinstance(n.op, ast.RShift) and 0 <= b <= 63: value = a >> b
            elif isinstance(n.op, ast.BitOr): value = a | b
            elif isinstance(n.op, ast.BitAnd): value = a & b
            else: raise MemoryEvidenceError("unsupported numeric operator")
        else:
            raise MemoryEvidenceError("unresolved linker expression")
        if abs(value) > MAX_ADDRESS:
            raise MemoryEvidenceError("linker expression overflows 64 bits")
        return value
    return visit(tree)


def _clean_script(text):
    # Preserve line numbers and offsets, including multi-line comments.
    text = re.sub(r"/\*.*?\*/", lambda m: "".join("\n" if c == "\n" else " " for c in m[0]), text, flags=re.S)
    return re.sub(r"(?://|#)[^\n]*", lambda m: " " * len(m[0]), text)


def parse_linker_script(text, source, *, dram=False):
    out = Parsed(format="memory-regions" if dram else "linker-script")
    clean = _clean_script(text)
    values = {}
    for m in re.finditer(r"(?m)^\s*([A-Za-z_]\w*)\s*=\s*([^;\n{}]+)\s*;", clean):
        try:
            values[m[1]] = _expression(m[2], values)
        except MemoryEvidenceError:
            pass  # Referencing this unresolved symbol below produces a visible unknown.
    blocks = list(re.finditer(r"\bMEMORY\s*\{([^{}]*)\}", clean, re.S))
    for block in blocks:
        body = block[1]
        region_re = re.compile(r"([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*:\s*"
                               r"(?:ORIGIN|org|o)\s*=\s*([^,\n]+)\s*,\s*"
                               r"(?:LENGTH|len|l)\s*=\s*([^;\n}]+)", re.I)
        consumed = list(body)
        for m in region_re.finditer(body):
            number = clean.count("\n", 0, block.start(1) + m.start()) + 1
            ev = _line(source, number, text.splitlines()[number - 1])
            try:
                start, size = _expression(m[2], values), _expression(m[3], values)
                out.add(Interval(m[1], start, size, "lma" if dram else "vma", "region", ev))
            except MemoryEvidenceError as exc:
                out.uncertain(f"region {m[1]}: {exc}", ev)
            consumed[m.start():m.end()] = " " * (m.end() - m.start())
        if "".join(consumed).strip(" \n\r\t;"):
            out.uncertain("unparsed MEMORY declaration", source)
    if not blocks:
        out.uncertain("no complete MEMORY block; region constraints are unknown", source)
    if dram:
        return out
    # Match output section/group blocks with balanced braces, preserving placement
    # inherited from MetaWare GROUPs. We do not evaluate linker wildcard commands.
    starts = []
    stack = []
    section_header = re.compile(r"([A-Za-z_.$][\w.$-]*)(.*?)\s*:\s*(?:AT\s*\([^)]*\)\s*)?$", re.S)
    previous = 0
    for i, c in enumerate(clean):
        if c == "{":
            prefix = clean[previous:i].rsplit(";", 1)[-1].strip()
            prefix = re.sub(r"^>\s*[A-Za-z_]\w*\s*", "", prefix)
            prefix = re.sub(r"^AT\s*>\s*[A-Za-z_]\w*\s*", "", prefix, flags=re.I)
            prefix_lines = [line.strip() for line in prefix.splitlines() if line.strip()]
            prefix = prefix_lines[-1] if prefix_lines else ""
            m = section_header.search(prefix)
            if not m and ":" in prefix:
                number = clean.count("\n", 0, i) + 1
                out.uncertain("unsupported linker section declaration", _line(source, number, prefix))
            # A nested .text {...} is a MetaWare input specification unless it has
            # its own colon; only explicit output declarations get constraints.
            entry = {"open": i, "name": m[1] if m else "", "header": m[2] if m else "",
                     "parent": stack[-1] if stack else None,
                     "load_expression": (re.search(r":\s*AT\s*\(([^)]+)\)\s*$", prefix) or [None, ""])[1]}
            stack.append(entry)
            previous = i + 1
        elif c == "}":
            if not stack:
                out.uncertain("unbalanced linker script braces", source)
                continue
            entry = stack.pop()
            tail = clean[i + 1:]
            placement = re.match(r"\s*>\s*([A-Za-z_]\w*)", tail)
            load = re.match(r"\s*(?:>\s*\w+\s*)?AT\s*>\s*([A-Za-z_]\w*)", tail, re.I)
            entry.update(region=placement[1] if placement else "", lma_region=load[1] if load else "")
            starts.append(entry)
            previous = i + 1
        elif c in ";\n" and not stack:
            previous = i + 1
    if stack:
        out.uncertain("unbalanced linker script braces", source)
    for entry in starts:
        name = entry["name"]
        if not name:
            continue
        if name == "GROUP":
            if entry["header"].strip():
                out.uncertain("GROUP address/alignment constraints require group layout evaluation", source)
            continue
        parent = entry["parent"]
        region = entry.get("region", "")
        while not region and parent:
            region = parent.get("region", "")
            parent = parent["parent"]
        number = clean.count("\n", 0, entry["open"]) + 1
        ev = _line(source, number, text.splitlines()[number - 1])
        header = entry["header"].strip()
        address = alignment = lma_address = None
        align = re.search(r"\bALIGN\s*\(([^)]+)\)", header)
        try:
            if align:
                alignment = _expression(align[1], values)
                header = header.replace(align[0], "").strip()
            header = re.sub(r"\((?:NOLOAD|READONLY)\)", "", header).strip()
            if header:
                address = _expression(header, values)
            if entry["load_expression"]:
                lma_address = _expression(entry["load_expression"], values)
        except MemoryEvidenceError as exc:
            out.uncertain(f"section {name}: {exc}", ev)
        out.constraints.append({"name": name, "region": region,
                                "lma_region": entry.get("lma_region", ""),
                                "address": address, "alignment": alignment,
                                "lma_address": lma_address,
                                "evidence": ev})
    for n, line in enumerate(clean.splitlines(), 1):
        if re.search(r"\b(?:INCLUDE|OVERLAY|REGION_ALIAS|IF|INITDATA)\b", line):
            out.uncertain("linker directive requires expansion; not evaluated", _line(source, n, line))
    return out


def parse_regions(text, source):
    if not text.lstrip().startswith("{"):
        return parse_linker_script(text, source, dram=True)
    data = _json(text)
    if (not isinstance(data, dict) or set(data) - {"schema", "regions", "complete"}
            or not {"schema", "regions"} <= set(data) or type(data["schema"]) is not int or data["schema"] != 1):
        raise MemoryEvidenceError("DRAM JSON requires schema=1 and regions")
    if type(data.get("complete", False)) is not bool:
        raise MemoryEvidenceError("DRAM complete must be a boolean")
    if not isinstance(data["regions"], list) or not data["regions"]:
        raise MemoryEvidenceError("DRAM regions must be a nonempty list")
    out = Parsed(format="dram-json-v1")
    out.metadata["complete"] = data.get("complete", False)
    names = set()
    for i, row in enumerate(data["regions"]):
        if not isinstance(row, dict) or set(row) - {"name", "start", "size", "space", "reserved", "alignment"}:
            raise MemoryEvidenceError("invalid DRAM region fields")
        if not {"name", "start", "size", "space"} <= set(row):
            raise MemoryEvidenceError("DRAM region requires name/start/size/space")
        if not isinstance(row["name"], str) or not row["name"] or row["name"] in names:
            raise MemoryEvidenceError("DRAM region names must be unique nonempty strings")
        names.add(row["name"])
        if type(row.get("reserved", False)) is not bool:
            raise MemoryEvidenceError("reserved must be a boolean")
        ev = Evidence(source.source, source.sha256, f"regions[{i}]", json.dumps(row, ensure_ascii=False))
        out.add(Interval(row["name"], _integer(row["start"]), _integer(row["size"]), row["space"],
                         "reserved" if row.get("reserved", False) else "region", ev,
                         _integer(row.get("alignment", 1))))
    return out


def parse_preload(text, source, *, root=None):
    out = Parsed(format="preload-records")
    if text.lstrip().startswith("{"):
        data = _json(text)
        if (not isinstance(data, dict) or set(data) != {"schema", "records"}
                or type(data["schema"]) is not int or data["schema"] != 1):
            raise MemoryEvidenceError("preload JSON requires schema=1 and records")
        if not isinstance(data["records"], list):
            raise MemoryEvidenceError("preload records must be a list")
        out.format = "preload-json-v1"
        for i, row in enumerate(data["records"]):
            if not isinstance(row, dict) or set(row) - {"name", "start", "size", "space", "operation", "file"}:
                raise MemoryEvidenceError("invalid preload record fields")
            if not {"start", "size", "space", "operation"} <= set(row):
                raise MemoryEvidenceError("preload record requires start/size/space/operation")
            if row["operation"] not in ("load", "zero"):
                raise MemoryEvidenceError("preload operation must be load or zero")
            ev = Evidence(source.source, source.sha256, f"records[{i}]", json.dumps(row, ensure_ascii=False))
            size = _integer(row["size"])
            if row.get("file"):
                if root is None:
                    raise MemoryEvidenceError("preload files require a sandbox root")
                b, artifact = read_source(root, row["file"], limit=MAX_ELF_BYTES)
                out.metadata.setdefault("artifacts", []).append(artifact)
                if len(b) != size:
                    out.uncertain("preload declared size differs from its artifact byte length", ev)
            out.add(Interval(str(row.get("name", f"record[{i}]")), _integer(row["start"]), size,
                             row["space"], row["operation"], ev))
    else:
        for n, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "//")):
                continue
            ev = _line(source, n, line)
            m = re.fullmatch(r"Loading section (\S+), size (0x[0-9a-fA-F]+) lma (0x[0-9a-fA-F]+)", stripped)
            if m:
                out.format = "gdb-load-log"
                out.add(Interval(m[1], int(m[3], 16), int(m[2], 16), "lma", "load", ev))
                continue
            if stripped.startswith("--preload"):
                try:
                    tokens = shlex.split(stripped)
                except ValueError:
                    tokens = []
                if len(tokens) == 3 and tokens[0] == "--preload" and root is not None:
                    b, artifact = read_source(root, tokens[2], limit=MAX_ELF_BYTES)
                    out.metadata.setdefault("artifacts", []).append(artifact)
                    out.add(Interval(tokens[2], _integer(tokens[1]), len(b), "lma", "load", ev,
                                     metadata={"artifact": asdict(artifact), "declared_only": True}))
                    out.uncertain("--preload is a load declaration, not proof of execution", ev)
                    continue
            # GDB's summary carries no new interval. Other nonempty lines remain visible.
            if re.match(r"^(Start address |Transfer rate:|Transfer size:)", stripped):
                continue
            out.uncertain("unrecognized preload record; no address/size inferred", ev)
    if not out.intervals:
        out.uncertain("no preload load/zero intervals were parsed", source)
    return out


def _intersection(a, b):
    if a.space != b.space or not a.size or not b.size:
        return None
    start, end = max(a.start, b.start), min(a.end, b.end)
    return (start, end) if start < end else None


def _uncovered(interval, coverage):
    cursor = interval.start
    missing = []
    for other in sorted((x for x in coverage if x.space == interval.space and x.size), key=lambda x: x.start):
        if other.end <= cursor or other.start >= interval.end:
            continue
        if other.start > cursor:
            missing.append((cursor, min(other.start, interval.end)))
        cursor = max(cursor, min(other.end, interval.end))
    if cursor < interval.end:
        missing.append((cursor, interval.end))
    return missing


def compare_layout(elf: Parsed, *, linker_map=None, linker_script=None, preload=None, dram=None):
    """Compare evidence without equating segment containment with section overlap."""
    issues = []
    unknown = list(elf.unknown)
    checks = 0
    architecture = {k: elf.metadata[k] for k in ("machine", "bits", "type", "byte_length") if k in elf.metadata}
    def issue(rule, message, *items, ranges=None):
        if len(issues) >= MAX_ISSUES:
            raise MemoryEvidenceError("too many conflicts; narrow or repair the inputs")
        issues.append({"rule": rule, "message": message, "intervals": [x.public() for x in items],
                       "ranges": [{"start": a, "end": b, "range": f"[0x{a:x}, 0x{b:x})"}
                                  for a, b in (ranges or [])]})
    inputs = {"elf": elf, "linker_map": linker_map, "linker_script": linker_script,
              "preload_log": preload, "dram_config": dram}
    for key, value in inputs.items():
        if key == "elf":
            continue
        if value is None:
            unknown.append({"reason": f"{key} was not supplied", "evidence": None})
        else:
            unknown.extend(value.unknown)
    sections = [x for x in elf.intervals if x.kind == "section"]
    segments = [x for x in elf.intervals if x.kind == "segment"]
    loads = [x for x in elf.intervals if x.kind == "load"]
    section_loads = [x for x in elf.intervals if x.kind == "section_load"]
    bits = elf.metadata.get("bits", 64)
    if elf.metadata.get("type") not in ("ET_EXEC", "ET_DYN"):
        return {"schema": 1, "status": "unknown", "architecture": architecture,
                "checks": 0, "conflicts": [], "unknown": unknown,
                "formats": {k: v.format if v else "not supplied" for k, v in inputs.items()},
                "interval_convention": "[start, end)",
                "inputs": {k: len(v.intervals) if v else 0 for k, v in inputs.items()}}
    for item in elf.intervals:
        checks += 1
        if item.end > 1 << bits:
            issue("address_width", f"interval exceeds ELF {bits}-bit address space", item)
        if item.kind == "section" and item.alignment > 1 and item.start % item.alignment:
            issue("alignment", "section address violates sh_addralign", item)
        if item.kind == "segment":
            m = item.metadata
            if m["filesz"] > item.size:
                issue("filesz_memsz", "PT_LOAD filesz exceeds memsz", item)
            if m["offset"] + m["filesz"] > elf.metadata.get("byte_length", MAX_ADDRESS):
                issue("file_bounds", "PT_LOAD file interval exceeds ELF byte length", item)
            if item.alignment > 1 and item.start % item.alignment != m["offset"] % item.alignment:
                issue("alignment", "PT_LOAD vaddr/offset are not congruent modulo p_align", item)
    for section in sections:
        if section.size and not any(s.start <= section.start and section.end <= s.end for s in segments):
            issue("segment_coverage", "allocated section is not contained in a PT_LOAD memory interval", section)
    # Compare peer allocations; segments contain sections by design and are never
    # compared against them as if they were independent allocations.
    def overlaps(items, rule):
        ordered = sorted(items, key=lambda x: (x.space, x.start, x.end))
        active = []
        for item in ordered:
            active = [x for x in active if x.space == item.space and x.end > item.start]
            for earlier in active:
                intersection = _intersection(earlier, item)
                if intersection:
                    issue(rule, "peer intervals overlap; no alias/overlay contract supplied", earlier, item,
                          ranges=[intersection])
            if item.size:
                active.append(item)
    overlaps(sections, "section_overlap")
    if linker_map is not None:
        names = set(elf.metadata.get("section_names", []))
        if names:
            for record in linker_map.intervals:
                if record.kind == "map_section" and record.size and record.name not in names:
                    checks += 1
                    issue("map_extra_section", "nonempty map output section is absent from ELF section inventory", record)
                    issues[-1]["sources"] = [elf.metadata["source"]]
        for section in sections + section_loads:
            kind = "map_section" if section.space == "vma" else "map_load"
            matches = [x for x in linker_map.intervals if x.name == section.name and x.kind == kind]
            if not matches:
                if kind == "map_section" and section.size:
                    unknown.append({"reason": f"ELF section {section.name} missing from linker map",
                                    "evidence": asdict(section.evidence)})
                elif kind == "map_load" and section.size:
                    unknown.append({"reason": f"map does not provide LMA for {section.name}",
                                    "evidence": asdict(section.evidence)})
                continue
            if len(matches) != 1:
                unknown.append({"reason": f"ambiguous map output section {section.name}",
                                "evidence": asdict(section.evidence)})
                continue
            checks += 1
            other = matches[0]
            if (section.start, section.size) != (other.start, other.size):
                issue("map_mismatch", "ELF and map address/length differ", section, other)
            if other.metadata.get("type") == "bss" and not section.metadata.get("nobits"):
                issue("map_initialization", "map says bss but ELF section is file-backed", section, other)
    if linker_script is not None:
        regions = {x.name: x for x in linker_script.intervals}
        for c in linker_script.constraints:
            for section in (x for x in sections if x.name == c["name"]):
                checks += 1
                evidence_interval = Interval(c["name"], c["address"] if c["address"] is not None else section.start,
                                             section.size, "vma", "script_constraint", c["evidence"])
                if c["address"] is not None and c["address"] != section.start:
                    issue("script_address", "ELF address differs from explicit linker address", section, evidence_interval)
                if c["alignment"] is not None:
                    alignment = c["alignment"]
                    if alignment <= 0 or alignment & (alignment - 1):
                        unknown.append({"reason": "unsupported script alignment", "evidence": asdict(c["evidence"])})
                    elif section.start % alignment:
                        issue("script_alignment", f"ELF address is not aligned to {alignment}", section, evidence_interval)
                region = regions.get(c["region"])
                if region:
                    gaps = _uncovered(section, [region])
                    if gaps:
                        issue("script_bounds", "section exceeds its assigned linker MEMORY region", section, region, ranges=gaps)
                elif c["region"]:
                    unknown.append({"reason": f"unknown linker region {c['region']}", "evidence": asdict(c["evidence"])})
                lma_region = regions.get(c["lma_region"])
                if c.get("lma_address") is not None:
                    for load in (x for x in section_loads if x.name == section.name):
                        if load.start != c["lma_address"]:
                            declared = Interval(load.name, c["lma_address"], load.size, "lma", "script_constraint", c["evidence"])
                            issue("script_load_address", "ELF LMA differs from explicit AT address", load, declared)
                if lma_region:
                    physical = Interval(lma_region.name, lma_region.start, lma_region.size, "lma", "region", lma_region.evidence)
                    for load in (x for x in section_loads if x.name == section.name):
                        gaps = _uncovered(load, [physical])
                        if gaps:
                            issue("script_load_bounds", "section LMA exceeds AT> MEMORY region", load, physical, ranges=gaps)
    if preload is not None:
        overlaps(preload.intervals, "preload_overlap")
        load_records = [x for x in preload.intervals if x.kind == "load"]
        zero_records = [x for x in preload.intervals if x.kind == "zero"]
        effective_loads = []
        unresolved_mapping = False
        for record in load_records:
            if record.space == "lma":
                effective_loads.append(record)
            elif record.space == "vma" and any(
                s.start == s.metadata["paddr"] and s.start <= record.start
                and record.end <= s.start + s.metadata["filesz"] for s in segments
            ):
                effective_loads.append(Interval(record.name, record.start, record.size,
                                               "lma", record.kind, record.evidence))
            else:
                unresolved_mapping = True
                unknown.append({"reason": "preload address space requires an explicit physical mapping",
                                "evidence": asdict(record.evidence)})
        for expected in section_loads:
            checks += 1
            gaps = _uncovered(expected, effective_loads)
            if gaps:
                if unresolved_mapping or preload.unknown:
                    unknown.append({"reason": "preload coverage is unknown because records or physical mapping are incomplete",
                                    "evidence": asdict(expected.evidence),
                                    "ranges": [{"start": a, "end": b} for a, b in gaps]})
                else:
                    issue("preload_missing", "file-backed ELF section is not fully covered by preload records", expected, ranges=gaps)
        for record in effective_loads:
            gaps = _uncovered(record, loads)
            if gaps:
                issue("preload_extra", "preload bytes extend beyond ELF file-backed PT_LOAD ranges", record, ranges=gaps)
        zero_seen = set()
        for expected in (x for x in elf.intervals if x.kind == "zero" and x.size):
            key = (expected.start, expected.end)
            if key in zero_seen:
                continue
            zero_seen.add(key)
            checks += 1
            identity_mapping = any(s.start == s.metadata["paddr"] and s.start <= expected.start
                                   and expected.end <= s.end for s in segments)
            effective_zeros = list(zero_records)
            if identity_mapping:
                effective_zeros.extend(Interval(x.name, x.start, x.size, "vma", x.kind, x.evidence)
                                       for x in zero_records if x.space == "lma")
            gaps = _uncovered(expected, effective_zeros)
            if gaps:
                unknown.append({"reason": "zero initialization is not evidenced for " + expected.public()["range"],
                                "evidence": asdict(expected.evidence),
                                "ranges": [{"start": a, "end": b} for a, b in gaps]})
            for record in load_records:
                mapped_record = (Interval(record.name, record.start, record.size, "vma", record.kind, record.evidence)
                                 if identity_mapping and record.space == "lma" else record)
                intersection = _intersection(expected, mapped_record)
                if intersection:
                    issue("zero_overwritten", "load record writes into a required zero-initialized interval", expected, record,
                          ranges=[intersection])
    if dram is not None:
        regions = [x for x in dram.intervals if x.kind == "region"]
        reserved = [x for x in dram.intervals if x.kind == "reserved"]
        overlaps(regions, "dram_overlap")
        physical_memory = []
        for segment in segments:
            # p_paddr + memsz is not a physical allocation when VMA != LMA (ROM
            # image copied to RAM). Only identity mappings justify that conversion.
            if segment.start == segment.metadata["paddr"]:
                physical_memory.append(Interval(segment.name, segment.start, segment.size, "lma", "runtime_memory", segment.evidence))
        applicable = segments + loads + physical_memory + (preload.intervals if preload else [])
        for item in applicable:
            if not item.size:
                continue
            for hole in reserved:
                intersection = _intersection(item, hole)
                if intersection:
                    issue("reserved_overlap", "interval overlaps a reserved memory region", item, hole, ranges=[intersection])
            same_space = [x for x in regions if x.space == item.space]
            if not same_space:
                unknown.append({"reason": f"no DRAM regions declared for {item.space}", "evidence": asdict(item.evidence)})
                continue
            checks += 1
            gaps = _uncovered(item, same_space)
            if gaps:
                if dram.metadata.get("complete") or any(_intersection(item, region) for region in same_space):
                    issue("dram_bounds", "interval exceeds supplied usable memory regions", item, *same_space, ranges=gaps)
                else:
                    unknown.append({"reason": "interval is outside the declared DRAM scope; other hardware memory is unspecified",
                                    "evidence": asdict(item.evidence), "interval": item.public()})
    # Stable deduplication keeps a missing fact visible without flooding the report.
    unique = {json.dumps(x, sort_keys=True): x for x in unknown}
    unknown = list(unique.values())
    return {"schema": 1, "status": "conflict" if issues else "unknown" if unknown else "pass",
            "architecture": architecture, "checks": checks, "conflicts": issues, "unknown": unknown,
            "formats": {k: v.format if v else "not supplied" for k, v in inputs.items()},
            "interval_convention": "[start, end); END in MetaWare input is inclusive",
            "inputs": {k: len(v.intervals) if v else 0 for k, v in inputs.items()}}


def analyze(root, path, *, linker_map="", linker_script="", preload_log="", dram_config="", map_format="auto"):
    raw, source = read_source(root, path, limit=MAX_ELF_BYTES)
    elf = parse_elf(raw, source)
    parsed = {}
    identities = [(source, MAX_ELF_BYTES)]
    for key, name in (("linker_map", linker_map), ("linker_script", linker_script),
                      ("preload", preload_log), ("dram", dram_config)):
        if not name:
            parsed[key] = None
            continue
        data, evidence = read_source(root, name)
        identities.append((evidence, MAX_TEXT_BYTES))
        try:
            text = data.decode("utf-8-sig", errors="strict")
        except UnicodeError as exc:
            raise MemoryEvidenceError("memory evidence text is not valid UTF-8") from exc
        if key == "linker_map": parsed[key] = parse_linker_map(text, evidence, format=map_format)
        elif key == "linker_script": parsed[key] = parse_linker_script(text, evidence)
        elif key == "preload": parsed[key] = parse_preload(text, evidence, root=root)
        else: parsed[key] = parse_regions(text, evidence)
        identities.extend((artifact, MAX_ELF_BYTES) for artifact in parsed[key].metadata.get("artifacts", []))
    report = compare_layout(elf, **parsed)
    # Re-read all inputs after analysis: evidence from different revisions must not
    # be presented as one coherent snapshot. No application state is written.
    for item, limit in identities:
        _, current = read_source(root, item.source, limit=limit)
        if current.sha256 != item.sha256:
            raise MemoryEvidenceError("evidence changed during consistency analysis; retry")
    report["sources"] = [asdict(x) for x, _ in identities]
    return report


def render(report, *, max_chars=25000):
    lines = [f"Memory consistency: {report['status']}; {report['checks']} checks; "
             f"{len(report['conflicts'])} conflicts; {len(report['unknown'])} unknown",
             "Intervals: [start, end), with separate VMA/LMA address spaces.",
             "Formats: " + json.dumps(report["formats"], ensure_ascii=False)]
    for issue in report["conflicts"]:
        lines.append(f"CONFLICT [{issue['rule']}] {issue['message']}")
        for ev in issue.get("sources", []):
            lines.append(f"  inventory source: {ev['source']}:{ev['locator']} sha256={ev['sha256']}")
        for item in issue["intervals"]:
            ev = item["evidence"]
            lines.append(f"  {item['space']} {item['name']} {item['range']} length=0x{item['length']:x} "
                         f"<- {ev['source']}:{ev['locator']} sha256={ev['sha256']}")
            if ev["text"]:
                lines.append("    evidence: " + ev["text"])
        for piece in issue["ranges"]:
            lines.append("  conflicting/uncovered interval: " + piece["range"])
    for item in report["unknown"]:
        ev = item.get("evidence")
        suffix = f" <- {ev['source']}:{ev['locator']}" if ev else ""
        lines.append("UNKNOWN " + item["reason"] + suffix)
    text = "\n".join(lines)
    if len(text) > max_chars:
        note = "\n[Truncated: conflict/unknown totals above include omitted records; narrow the evidence inputs.]"
        text = text[:max(0, max_chars - len(note))] + note
    return text
