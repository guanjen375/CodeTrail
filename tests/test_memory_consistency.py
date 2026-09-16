"""Silent wrong-answer and sandbox contracts for multi-source memory evidence."""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

import memory_consistency as memory

pytestmark = pytest.mark.smoke


def _evidence(name="test.map", line="line 1"):
    return memory.Evidence(name, "a" * 64, line)


def _elf():
    """A synthetic identity-mapped ELF32 ARM executable, generated without a compiler."""
    names = b"\0.text\0.bss\0.shstrtab\0"
    ident = b"\x7fELF" + bytes([1, 1, 1]) + bytes(9)
    header = ident + struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0x1000, 52, 0x140,
                                 0, 52, 32, 1, 40, 4, 3)
    program = struct.pack("<IIIIIIII", 1, 0x100, 0x1000, 0x1000, 4, 12, 7, 4)
    result = bytearray(header + program)
    result.extend(bytes(0x100 - len(result)))
    result.extend(b"\x00\x00\xa0\xe1")  # Arbitrary four initialized bytes, never executed.
    result.extend(names)
    result.extend(bytes(0x140 - len(result)))
    result.extend(bytes(40))
    result.extend(struct.pack("<10I", 1, 1, 6, 0x1000, 0x100, 4, 0, 0, 4, 0))
    result.extend(struct.pack("<10I", 7, 8, 3, 0x1004, 0x104, 8, 0, 0, 4, 0))
    result.extend(struct.pack("<10I", 12, 3, 0, 0, 0x104, len(names), 0, 0, 1, 0))
    return bytes(result)


def _inputs(root):
    (root / "test.elf").write_bytes(_elf())
    (root / "test.map").write_text("Linker script and memory map\n.text 0x1000 0x4\n.bss 0x1004 0x8\n")
    (root / "test.ld").write_text("MEMORY { RAM : ORIGIN = 0x1000, LENGTH = 0x100 }\n"
                                 "SECTIONS { .text : { *(.text) } > RAM\n.bss : { *(.bss) } > RAM }\n")
    (root / "preload.json").write_text(json.dumps({"schema": 1, "records": [
        {"name": ".text", "start": "0x1000", "size": 4, "space": "lma", "operation": "load"},
        {"name": ".bss", "start": "0x1004", "size": 8, "space": "vma", "operation": "zero"},
    ]}))
    (root / "dram.json").write_text(json.dumps({"schema": 1, "complete": True, "regions": [
        {"name": "RAM-vma", "start": "0x1000", "size": 256, "space": "vma"},
        {"name": "RAM-lma", "start": "0x1000", "size": 256, "space": "lma"},
    ]}))
    return dict(linker_map="test.map", linker_script="test.ld", preload_log="preload.json", dram_config="dram.json")


def test_consistency_full_evidence_and_missing_zero_are_distinct(tmp_path):
    args = _inputs(tmp_path)
    report = memory.analyze(tmp_path, "test.elf", **args)
    assert report["status"] == "pass", report
    assert report["architecture"]["machine"] == "EM_ARM"
    raw = json.loads((tmp_path / "preload.json").read_text())
    raw["records"].pop()
    (tmp_path / "preload.json").write_text(json.dumps(raw))
    report = memory.analyze(tmp_path, "test.elf", **args)
    assert report["status"] == "unknown"
    assert any("zero initialization" in x["reason"] for x in report["unknown"])


def test_consistency_reports_exact_ranges_and_source_for_bounds(tmp_path):
    args = _inputs(tmp_path)
    raw = json.loads((tmp_path / "dram.json").read_text())
    raw["regions"][0]["size"] = 8
    (tmp_path / "dram.json").write_text(json.dumps(raw))
    report = memory.analyze(tmp_path, "test.elf", **args)
    issue = next(x for x in report["conflicts"] if x["rule"] == "dram_bounds")
    assert issue["ranges"] == [{"start": 0x1008, "end": 0x100c, "range": "[0x1008, 0x100c)"}]
    assert {x["evidence"]["source"] for x in issue["intervals"]} == {"test.elf", "dram.json"}


def test_consistency_vma_lma_are_not_compared_as_same_address_space():
    a = memory.Interval("RAM", 0x1000, 16, "vma", "section", _evidence())
    b = memory.Interval("ROM", 0x1000, 16, "lma", "load", _evidence())
    assert memory._intersection(a, b) is None
    assert memory._uncovered(a, [b]) == [(0x1000, 0x1010)]
    adjacent = memory.Interval("next", 0x1010, 4, "vma", "section", _evidence())
    assert memory._intersection(a, adjacent) is None


def test_consistency_unrecognized_or_partial_input_cannot_pass(tmp_path):
    args = _inputs(tmp_path)
    (tmp_path / "test.map").write_text("firmware memory map\n.text 1000 4\n")
    report = memory.analyze(tmp_path, "test.elf", **args)
    assert report["status"] == "unknown"
    assert any("not recognized" in x["reason"] for x in report["unknown"])
    incomplete = memory.parse_linker_map("SECTION SUMMARY\nOUTPUT/ TYPE START END\nINPUT SECTION ADDRESS ADDRESS LENGTH\n"
                                         ".text text 00001000 00001009 00000004\n", _evidence())
    assert any("inclusive END" in x["reason"] for x in incomplete.unknown)


def test_consistency_secondary_sources_reject_symlink_and_hardlink(tmp_path):
    args = _inputs(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + "-outside.map")
    outside.write_text("Linker script and memory map\n.text 0x1000 0x4\n")
    link = tmp_path / "unsafe.map"
    link.symlink_to(outside)
    with pytest.raises(memory.MemoryEvidenceError, match="unsafe"):
        memory.analyze(tmp_path, "test.elf", **(args | {"linker_map": "unsafe.map"}))
    link.unlink()
    link.hardlink_to(outside)
    with pytest.raises(memory.MemoryEvidenceError, match="hard links"):
        memory.analyze(tmp_path, "test.elf", **(args | {"linker_map": "unsafe.map"}))


def test_consistency_preload_declaration_does_not_prove_execution(tmp_path):
    (tmp_path / "bytes.bin").write_bytes(b"1234")
    parsed = memory.parse_preload("--preload 0x1000 bytes.bin\n", _evidence(), root=tmp_path)
    assert parsed.intervals[0].size == 4
    assert any("not proof of execution" in x["reason"] for x in parsed.unknown)
    with pytest.raises(memory.MemoryEvidenceError):
        memory.parse_preload(json.dumps({"schema": 1, "records": [
            {"start": "0xffffffffffffffff", "size": 2, "space": "lma", "operation": "load"}
        ]}), _evidence())


def test_consistency_metaware_inclusive_end_and_wrapped_name():
    text = "SECTION SUMMARY\nOUTPUT/ TYPE START END\nINPUT SECTION ADDRESS ADDRESS LENGTH\n" \
           "custom_section\n text 00002000 00002039 0000003a\n.bss bss 00003000 00003007 00000008\n"
    out = memory.parse_linker_map(text, _evidence())
    assert out.format == "metaware" and out.unknown == []
    assert out.intervals[0].end == 0x203a
    assert out.intervals[0].evidence.locator == "line 4; line 5"


def test_consistency_vma_preload_never_creates_an_unsupported_missing_claim(tmp_path):
    args = _inputs(tmp_path)
    raw = json.loads((tmp_path / "preload.json").read_text())
    raw["records"][0]["space"] = "vma"
    (tmp_path / "preload.json").write_text(json.dumps(raw))
    # The ELF proves identity mapping here, so VMA load evidence is sufficient.
    report = memory.analyze(tmp_path, "test.elf", **args)
    assert report["status"] == "pass", report
    # For different VMA/LMA, the missing mapping is unknown, not a proven gap.
    elf_bytes = bytearray(_elf())
    struct.pack_into("<I", elf_bytes, 52 + 12, 0x8000)
    (tmp_path / "test.elf").write_bytes(elf_bytes)
    report = memory.analyze(tmp_path, "test.elf", preload_log="preload.json")
    assert not any(i["rule"] == "preload_missing" for i in report["conflicts"])
    assert any("mapping" in i["reason"] for i in report["unknown"])


def test_consistency_map_only_section_and_reserved_only_region_are_not_missed(tmp_path):
    args = _inputs(tmp_path)
    (tmp_path / "test.map").write_text("Linker script and memory map\n.text 0x1000 0x4\n"
                                       ".bss 0x1004 0x8\n.unexpected 0x2000 0x10\n")
    report = memory.analyze(tmp_path, "test.elf", **args)
    assert any(i["rule"] == "map_extra_section" for i in report["conflicts"]), report
    (tmp_path / "reserved.json").write_text(json.dumps({"schema": 1, "regions": [
        {"name": "reserved", "start": "0x1000", "size": 4, "space": "vma", "reserved": True}
    ]}))
    report = memory.analyze(tmp_path, "test.elf", dram_config="reserved.json")
    assert any(i["rule"] == "reserved_overlap" for i in report["conflicts"]), report
