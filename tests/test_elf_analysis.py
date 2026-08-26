"""ELF 分析（elf_analysis / media.read_elf / analyze_file 的 ELF 視角）契約。

來源：2026-08-26 comment.md 列出的 11 條 ELF 分析缺口。依 AGENTS.md §2.4 這裡只放兩類：

1. 真實發生過的無聲失真（regression，標 smoke）：
   - summary 只列 GLOBAL/WEAK 且 size>0 的 symbol，firmware 常見的 LOCAL/static 函式、
     UND、size=0 的組語 label 被靜默排除（comment #4）
   - 註解宣稱結構化路徑可取得 relocation，但實作根本沒解析（comment #7）
   - 反組譯失敗時整段省略、不說原因；x86 主機的 objdump 對 ARM/RISC-V/ARC 韌體全部
     拿不到反組譯卻沒有任何提示（comment #8）
   - pyelftools 缺席時報告只標 parser 名字，不講哪些能力沒了（comment #2）
2. 無聲失敗風險的契約：
   - 未知 view 必須回錯誤並列出可用 view（不是默默當 summary）
   - hard cap 截斷必須帶說明（不是砍掉就算）
   - ingest_document 走長版報告（comment #11）：不能還是那份 25K 的 summary
   - analyze_file 要把 view / target / limit 傳到 ELF 路徑；非 ELF 檔案要明講已忽略

全部離線：用純 Python 手工組一個最小 ET_REL x86-64 ELF，不需要 gcc；只有 fallback
那條會呼叫 binutils readelf（缺就 skip）。
"""
from __future__ import annotations

import shutil
import struct
from pathlib import Path

import pytest

import elf_analysis
import media
from tests._harness import import_mcp_module


# ---------------------------------------------------------------------------
# 最小 ELF 產生器
# ---------------------------------------------------------------------------

def build_min_elf(path: Path) -> Path:
    """最小 ET_REL x86-64 ELF。

    .text    : helper_static(LOCAL FUNC, 8 bytes) / main(GLOBAL FUNC, 16 bytes) /
               asm_label(GLOBAL FUNC, size 0)
    .rodata  : 一條錯誤字串、一條 URL、一條路徑、一條版本字串（給字串分類用）
    .rela.text: main 內 offset 13 對 printf(UND) 的 R_X86_64_PLT32 -4
    """
    text = bytes([0x55, 0x48, 0x89, 0xE5, 0x5D, 0xC3, 0x90, 0x90])            # helper_static
    text += bytes([0x55, 0x48, 0x89, 0xE5, 0xE8, 0, 0, 0, 0, 0x31, 0xC0, 0x5D, 0xC3, 0x90, 0x90, 0x90])  # main
    text += bytes([0x90] * 8)                                                   # asm_label
    rodata = (b"error: bad thing %d\x00http://example.com/fw\x00/etc/fw.conf\x00"
              b"FW version 2.1 build 2026\x00")
    strtab = b"\x00demo.c\x00helper_static\x00main\x00asm_label\x00printf\x00"
    shstrtab = b"\x00.text\x00.rodata\x00.symtab\x00.strtab\x00.rela.text\x00.shstrtab\x00"

    def stroff(table: bytes, name: str) -> int:
        return table.index(b"\x00" + name.encode() + b"\x00") + 1

    SYM = "<IBBHQQ"
    STT_NOTYPE, STT_FUNC, STT_FILE = 0, 2, 4
    STB_LOCAL, STB_GLOBAL = 0, 1
    SHN_UNDEF, SHN_ABS = 0, 0xFFF1

    def sym(name: str, bind: int, typ: int, shndx: int, value: int, size: int) -> bytes:
        return struct.pack(SYM, stroff(strtab, name), (bind << 4) | typ, 0, shndx, value, size)

    symtab = struct.pack(SYM, 0, 0, 0, 0, 0, 0)
    symtab += sym("demo.c", STB_LOCAL, STT_FILE, SHN_ABS, 0, 0)          # 1
    symtab += sym("helper_static", STB_LOCAL, STT_FUNC, 1, 0, 8)         # 2
    symtab += sym("main", STB_GLOBAL, STT_FUNC, 1, 8, 16)                 # 3 (first global)
    symtab += sym("asm_label", STB_GLOBAL, STT_FUNC, 1, 24, 0)            # 4
    symtab += sym("printf", STB_GLOBAL, STT_NOTYPE, SHN_UNDEF, 0, 0)      # 5
    R_X86_64_PLT32 = 4
    rela = struct.pack("<QQq", 13, (5 << 32) | R_X86_64_PLT32, -4)

    off = 64

    def place(data: bytes, align: int) -> int:
        nonlocal off
        while off % align:
            off += 1
        start = off
        off += len(data)
        return start

    text_off = place(text, 16)
    rodata_off = place(rodata, 1)
    symtab_off = place(symtab, 8)
    strtab_off = place(strtab, 1)
    rela_off = place(rela, 8)
    shstr_off = place(shstrtab, 1)
    while off % 8:
        off += 1
    shoff = off

    SHT_NULL, SHT_PROGBITS, SHT_SYMTAB, SHT_STRTAB, SHT_RELA = 0, 1, 2, 3, 4
    SHF_ALLOC, SHF_EXEC, SHF_INFO_LINK = 2, 4, 0x40
    SHDR = "<IIQQQQIIQQ"
    shdrs = [
        struct.pack(SHDR, 0, SHT_NULL, 0, 0, 0, 0, 0, 0, 0, 0),
        struct.pack(SHDR, stroff(shstrtab, ".text"), SHT_PROGBITS, SHF_ALLOC | SHF_EXEC, 0, text_off, len(text), 0, 0, 16, 0),
        struct.pack(SHDR, stroff(shstrtab, ".rodata"), SHT_PROGBITS, SHF_ALLOC, 0, rodata_off, len(rodata), 0, 0, 1, 0),
        struct.pack(SHDR, stroff(shstrtab, ".symtab"), SHT_SYMTAB, 0, 0, symtab_off, len(symtab), 4, 3, 8, 24),
        struct.pack(SHDR, stroff(shstrtab, ".strtab"), SHT_STRTAB, 0, 0, strtab_off, len(strtab), 0, 0, 1, 0),
        struct.pack(SHDR, stroff(shstrtab, ".rela.text"), SHT_RELA, SHF_INFO_LINK, 0, rela_off, len(rela), 3, 1, 8, 24),
        struct.pack(SHDR, stroff(shstrtab, ".shstrtab"), SHT_STRTAB, 0, 0, shstr_off, len(shstrtab), 0, 0, 1, 0),
    ]
    e_ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
    ehdr = struct.pack("<16sHHIQQQIHHHHHH", e_ident, 1, 62, 1, 0, 0, shoff, 0, 64, 0, 0, 64, len(shdrs), 6)
    buf = bytearray(shoff + 64 * len(shdrs))
    buf[0:64] = ehdr
    for data, o in ((text, text_off), (rodata, rodata_off), (symtab, symtab_off),
                    (strtab, strtab_off), (rela, rela_off), (shstrtab, shstr_off)):
        buf[o:o + len(data)] = data
    buf[shoff:] = b"".join(shdrs)
    path.write_bytes(bytes(buf))
    return path


@pytest.fixture
def elf_path(tmp_path: Path) -> Path:
    media.set_sandbox_root(str(tmp_path), allow_external=False)
    elf_analysis._MODEL_CACHE.clear()
    return build_min_elf(tmp_path / "fw.elf")


# ---------------------------------------------------------------------------
# regression：comment #4 / #7 / #8 / #2
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_summary_lists_local_functions_und_and_zero_size(elf_path: Path):
    """comment #4：LOCAL/static 函式、UND、size=0 symbol 以前被 summary 靜默排除。"""
    out = media.read_elf(str(elf_path))
    assert "helper_static" in out, out            # LOCAL FUNC 進了 Top functions
    assert "LOCAL 1" in out                        # 統計要分開算
    assert "UND 1" in out                          # 未定義 symbol 有計數
    assert "asm_label" in out and "size=0" in out  # 組語 label（size 0）另列
    # 完整表：篩選語法要能單獨挑出 LOCAL FUNC 與 UND
    local_only = media.read_elf(str(elf_path), view="symbols", target="bind:LOCAL type:FUNC")
    assert "helper_static" in local_only and "main" not in local_only.split("【Symbols")[1].split("篩選語法")[0].replace("Symbols", "")
    und_only = media.read_elf(str(elf_path), view="symbols", target="ndx:UND")
    assert "printf" in und_only


@pytest.mark.smoke
def test_relocations_are_parsed_with_caller(elf_path: Path):
    """comment #7：註解宣稱有 relocation，實作沒解析 —— 現在要有 section 統計、逐筆與 caller。"""
    model = elf_analysis.load_model(elf_path)
    assert model.relocs and model.relocs[0]["count"] == 1
    assert model.relocs[0]["applies_to"] == ".text"
    summary = media.read_elf(str(elf_path))
    assert "【Relocations】" in summary and "R_X86_64_PLT32" in summary
    detail = media.read_elf(str(elf_path), view="relocs", target="printf")
    assert "printf" in detail and "(in main+0x5)" in detail, detail


@pytest.mark.smoke
def test_disasm_failure_is_explained_not_omitted(elf_path: Path, monkeypatch):
    """comment #8：反組譯失敗以前直接省略。沒有可用的 objdump / capstone 時要說明原因與補救。"""
    monkeypatch.setattr(elf_analysis, "_objdump_candidates", lambda machine: [])
    monkeypatch.setattr(elf_analysis, "_capstone_disasm",
                        lambda model, plan, limit: (False, "未安裝（python3 -m pip install capstone）"))
    out = media.read_elf(str(elf_path), view="disasm", target="main")
    assert "[反組譯不可用]" in out, out
    assert "capstone" in out and "補救" in out and "AICODE_OBJDUMP" in out
    # 找不到 symbol 也要講，不能空白
    missing = media.read_elf(str(elf_path), view="disasm", target="no_such_symbol")
    assert "找不到 symbol" in missing


@pytest.mark.smoke
def test_readelf_fallback_declares_missing_capabilities(elf_path: Path, monkeypatch):
    """comment #2：pyelftools 缺席時只標了 parser 名字。fallback 必須明列缺失能力與補救，
    且基本資料（LOCAL 函式、relocation）不能跟結構化路徑差一截。"""
    if not shutil.which("readelf"):
        pytest.skip("binutils readelf 不存在，無法驗 fallback")
    monkeypatch.setattr(elf_analysis, "_HAS_PYELFTOOLS", False)
    elf_analysis._MODEL_CACHE.clear()
    media._ELF_CACHE.clear()
    out = media.read_elf(str(elf_path))
    assert "readelf 文字解析" in out
    assert "缺少以下能力" in out and "pyelftools" in out, out
    assert "helper_static" in out and "【Relocations】" in out and "R_X86_64_PLT32" in out
    model = elf_analysis.load_model(elf_path)
    assert model.parser == "readelf" and model.missing


# ---------------------------------------------------------------------------
# 契約：view 錯誤 / hard cap / ingest 長版 / analyze_file 參數轉發
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_unknown_view_is_rejected_with_the_list(elf_path: Path):
    out = media.read_elf(str(elf_path), view="nope")
    assert out.startswith("[ELF 錯誤]"), out
    for v in elf_analysis.VIEWS:
        assert f"  {v}:" in out


def test_hard_cap_truncation_is_explained(elf_path: Path):
    out = elf_analysis.build_report(elf_path, "summary", max_chars=400)
    assert len(out) <= 400
    assert "報告已截斷" in out and "view" in out


@pytest.mark.smoke
def test_ingest_uses_long_form_report(elf_path: Path):
    """comment #11：入庫不能還是那份 25K summary。長版要有完整 symbol 表、relocation 逐筆與全部字串。"""
    summary = media.read_elf(str(elf_path))
    doc = media.read_binary_for_ingest(str(elf_path))
    assert len(doc) > len(summary)
    assert "【Symbols / .symtab】" in doc and "符合篩選" in doc      # 完整表（不是 Top N）
    assert "【Relocations 逐筆】" in doc and "(in main+0x5)" in doc
    assert "【字串】篩選 'cat:all'" in doc
    assert "【深入查看" not in doc                                      # 給模型看的 view 提示不入庫
    assert "【記憶體配置】" in doc


def test_strings_are_categorized_with_section(elf_path: Path):
    diag = media.read_elf(str(elf_path), view="strings", target="cat:diagnostic")
    assert "error: bad thing %d" in diag and "[.rodata]" in diag
    assert "http://example.com/fw" in media.read_elf(str(elf_path), view="strings", target="cat:url")
    assert "/etc/fw.conf" in media.read_elf(str(elf_path), view="strings", target="cat:path")
    assert "FW version 2.1" in media.read_elf(str(elf_path), view="strings", target="cat:version")


def test_section_dump_and_symbol_lookup(elf_path: Path):
    dump = media.read_elf(str(elf_path), view="sections", target=".rodata", limit=64)
    assert "hex dump" in dump and "error: bad thing" in dump
    # REL 檔的位址反查要拒絕（section 位址全是 0），不能亂配
    look = media.read_elf(str(elf_path), view="symbols", target="0x10")
    assert "REL 檔" in look


@pytest.fixture
def mcp_module(monkeypatch, tmp_path: Path):
    return import_mcp_module(monkeypatch, tmp_path)


def test_analyze_file_forwards_view_target_limit(mcp_module, tmp_path: Path, monkeypatch):
    build_min_elf(tmp_path / "fw.elf")
    calls: dict = {}

    def fake_read_elf(p, **kw):
        calls.update(kw)
        return "OK"

    monkeypatch.setattr(mcp_module, "read_elf", fake_read_elf)
    fn = getattr(mcp_module.analyze_file, "fn", mcp_module.analyze_file)
    assert fn("fw.elf", view="symbols", target="main", limit=5) == "OK"
    assert calls == {"view": "symbols", "target": "main", "limit": 5}

    # 非 ELF 檔給了 view → 回覆開頭要註明已忽略（不是默默丟掉）
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    monkeypatch.setattr(mcp_module, "ocr_image", lambda p: "IMG")
    out = fn("shot.png", view="symbols")
    assert out.startswith("[注意]") and out.endswith("IMG")
    # 預設參數 → 沒有任何前綴（既有 PDF dispatch 測試依賴這一點）
    assert fn("shot.png") == "IMG"
