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

import re
import shutil
import struct
import subprocess
import time
from pathlib import Path

import pytest

import config
import elf_analysis
import media
from tests._harness import import_mcp_module


# ---------------------------------------------------------------------------
# 最小 ELF 產生器
# ---------------------------------------------------------------------------

def build_min_elf(path: Path, extra_und: int = 0, extra_strings: int = 0, extra_relocs: int = 0) -> Path:
    """最小 ET_REL x86-64 ELF。

    .text    : helper_static(LOCAL FUNC, 8 bytes) / main(GLOBAL FUNC, 16 bytes) /
               asm_label(GLOBAL FUNC, size 0)
    .rodata  : 一條錯誤字串、一條 URL、一條路徑、一條版本字串（給字串分類用）
    .rela.text: main 內 offset 13 對 printf(UND) 的 R_X86_64_PLT32 -4
    .rela.rodata: 一條沒有 symbol 的 R_X86_64_RELATIVE（審核 #1：symbol / caller 皆空的 reloc）
    .rodata 另含一條 3000 個 'a' 的長字串（審核 #5：災難性回溯 regex 的受害者）
    """
    text = bytes([0x55, 0x48, 0x89, 0xE5, 0x5D, 0xC3, 0x90, 0x90])            # helper_static
    text += bytes([0x55, 0x48, 0x89, 0xE5, 0xE8, 0, 0, 0, 0, 0x31, 0xC0, 0x5D, 0xC3, 0x90, 0x90, 0x90])  # main
    text += bytes([0x90] * 8)                                                   # asm_label
    rodata = (b"error: bad thing %d\x00http://example.com/fw\x00/etc/fw.conf\x00"
              b"FW version 2.1 build 2026\x00" + b"a" * 3000 + b"!\x00")
    rodata += b"".join(f"str_{i:05d}_payload_text".encode() + b"\x00" for i in range(extra_strings))
    strtab = b"\x00demo.c\x00helper_static\x00main\x00asm_label\x00printf\x00"
    extra_names = [f"s{i}" for i in range(extra_und)]          # 短名稱的外部參照（審核五 #3）
    strtab += b"".join(n.encode() + b"\x00" for n in extra_names)
    shstrtab = (b"\x00.text\x00.rodata\x00.symtab\x00.strtab\x00.rela.text\x00.shstrtab\x00"
                b".rela.rodata\x00")

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
    for name in extra_names:
        symtab += sym(name, STB_GLOBAL, STT_NOTYPE, SHN_UNDEF, 0, 0)
    R_X86_64_PLT32, R_X86_64_RELATIVE = 4, 8
    rela = struct.pack("<QQq", 13, (5 << 32) | R_X86_64_PLT32, -4)
    rela += b"".join(struct.pack("<QQq", 8 + (i % 16), (5 << 32) | R_X86_64_PLT32, -4) for i in range(extra_relocs))
    rela_rodata = struct.pack("<QQq", 0, (0 << 32) | R_X86_64_RELATIVE, 0x10)

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
    rela_ro_off = place(rela_rodata, 8)
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
        struct.pack(SHDR, stroff(shstrtab, ".rela.rodata"), SHT_RELA, SHF_INFO_LINK, 0, rela_ro_off, len(rela_rodata), 3, 2, 8, 24),
    ]
    e_ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
    ehdr = struct.pack("<16sHHIQQQIHHHHHH", e_ident, 1, 62, 1, 0, 0, shoff, 0, 64, 0, 0, 64, len(shdrs), 6)
    buf = bytearray(shoff + 64 * len(shdrs))
    buf[0:64] = ehdr
    for data, o in ((text, text_off), (rodata, rodata_off), (symtab, symtab_off),
                    (strtab, strtab_off), (rela, rela_off), (rela_rodata, rela_ro_off),
                    (shstrtab, shstr_off)):
        buf[o:o + len(data)] = data
    buf[shoff:] = b"".join(shdrs)
    path.write_bytes(bytes(buf))
    return path


def build_arm_exec_elf(path: Path, n_words: int = 4000, n_null_phdrs: int = 0, n_load_phdrs: int = 0) -> Path:
    """最小 ARM（EM_ARM）ET_EXEC：一個 LOAD segment @0x08000000，開頭像 Cortex-M 向量表
    （word0 = 初始 SP、word1 = Thumb reset），後面塞 n_words 個同樣的 handler 位址——
    模擬「.text 全是程式碼、只有前面一小段是向量表」的韌體。"""
    words = [0x20001000, 0x08000101] + [0x08000101] * (n_words - 2)
    data = struct.pack("<%dI" % len(words), *words)
    shstrtab = b"\x00.text\x00.shstrtab\x00"
    ehdr_size, phdr_size, shdr_size = 52, 32, 40
    n_phdrs = 1 + n_null_phdrs + n_load_phdrs
    data_off = ehdr_size + phdr_size * n_phdrs
    shstr_off = data_off + len(data)
    shoff = shstr_off + len(shstrtab)
    while shoff % 4:
        shoff += 1
    e_ident = b"\x7fELF" + bytes([1, 1, 1, 0]) + b"\x00" * 8
    ehdr = struct.pack("<16sHHIIIIIHHHHHH", e_ident, 2, 40, 1, 0x08000101, ehdr_size, shoff,
                       0x05000200, ehdr_size, phdr_size, n_phdrs, shdr_size, 3, 2)
    phdr = struct.pack("<IIIIIIII", 1, data_off, 0x08000000, 0x08000000, len(data), len(data), 5, 4)
    phdr += struct.pack("<IIIIIIII", 0, 0, 0, 0, 0, 0, 0, 0) * n_null_phdrs
    for i in range(n_load_phdrs):        # 額外的 LOAD：各自不同的 VMA/LMA，內容都指到同一段檔案資料
        va = 0x08100000 + 0x1000 * i
        phdr += struct.pack("<IIIIIIII", 1, data_off, va, va, len(data), len(data), 5, 4)
    SHDR = "<IIIIIIIIII"
    shdrs = [
        struct.pack(SHDR, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        struct.pack(SHDR, 1, 1, 6, 0x08000000, data_off, len(data), 0, 0, 4, 0),
        struct.pack(SHDR, 7, 3, 0, 0, shstr_off, len(shstrtab), 0, 0, 1, 0),
    ]
    buf = bytearray(shoff + shdr_size * len(shdrs))
    buf[0:ehdr_size] = ehdr
    buf[ehdr_size:ehdr_size + len(phdr)] = phdr
    buf[data_off:data_off + len(data)] = data
    buf[shstr_off:shstr_off + len(shstrtab)] = shstrtab
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


@pytest.mark.smoke
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


@pytest.mark.smoke
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


# ---------------------------------------------------------------------------
# 2026-08-26 靜態審核回修（六條）
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_ingest_keeps_relocs_without_symbol(elf_path: Path):
    """審核 #1：ingest 用 regex "." 當「全部」，symbol / caller 皆空的 R_*_RELATIVE 不會入庫。"""
    doc = media.read_binary_for_ingest(str(elf_path))
    detail = doc.split("【Relocations 逐筆】", 1)[1]
    assert "R_X86_64_RELATIVE" in detail and "printf" in detail, detail[:600]
    # 明確的「全列」語法：target="*"
    out = media.read_elf(str(elf_path), view="relocs", target="*")
    assert "R_X86_64_RELATIVE" in out and "printf" in out


@pytest.mark.smoke
def test_readelf_fallback_reports_command_failure_not_stripped(elf_path: Path, monkeypatch):
    """審核 #2：fallback 吞掉 readelf 失敗 → -sW 失敗被報成 fully stripped、-rW 失敗報成沒有 relocation。"""
    if not shutil.which("readelf"):
        pytest.skip("binutils readelf 不存在，無法驗 fallback")
    monkeypatch.setattr(elf_analysis, "_HAS_PYELFTOOLS", False)
    real_run = elf_analysis.run_cmd

    def flaky(cmd, timeout=30):
        if cmd[0] == "readelf" and cmd[1] in ("-sW", "-rW"):
            return None, "timeout"
        return real_run(cmd, timeout)

    monkeypatch.setattr(elf_analysis, "run_cmd", flaky)
    elf_analysis._MODEL_CACHE.clear()
    media._ELF_CACHE.clear()
    out = media.read_elf(str(elf_path))
    assert "fully stripped" not in out, out
    assert "readelf -sW" in out and "timeout" in out
    relocs = media.read_elf(str(elf_path), view="relocs")
    assert "沒有 relocation section" not in relocs
    assert "readelf -rW" in relocs and "timeout" in relocs


@pytest.mark.smoke
def test_dwarf_kind_filter_without_regex_lists_all(tmp_path: Path):
    """審核 #3：文件說 target="kind:func" / "kind:type" 可用，實作沒有 regex 就退回 CU summary。"""
    gcc = shutil.which("gcc")
    if not gcc:
        pytest.skip("需要 gcc 產生帶 DWARF 的目標檔")
    src = tmp_path / "t.c"
    src.write_text(
        "struct pt { int x; int y; };\n"
        "int add(int a, int b) { return a + b; }\n"
        "int main(void) { struct pt p = {1, 2}; return add(p.x, p.y); }\n"
    )
    obj = tmp_path / "t.o"
    subprocess.run([gcc, "-g", "-O0", "-c", "-o", str(obj), str(src)], check=True, capture_output=True)
    media.set_sandbox_root(str(tmp_path), allow_external=False)
    elf_analysis._MODEL_CACHE.clear()
    funcs = media.read_elf(str(obj), view="dwarf", target="kind:func")
    assert "【DWARF 函式】" in funcs and " add " in funcs and " main " in funcs, funcs
    assert "個 CU；函式 DIE" not in funcs
    types = media.read_elf(str(obj), view="dwarf", target="kind:type")
    assert "【DWARF 型別】" in types and "struct pt" in types and "【DWARF 函式】" not in types, types


@pytest.mark.smoke
def test_ingest_budget_spreads_across_views(elf_path: Path):
    """審核 #4：入庫「完整長版」在全域上限前先被各段固定配額砍掉，後段資料永遠查不到。
    超過上限時要各段依比例截斷並註明，每一段都要留下。"""
    full = media.read_binary_for_ingest(str(elf_path))
    assert "此段截斷" not in full                      # 沒超過上限就是真的完整
    small = elf_analysis.build_ingest_document(elf_path, max_chars=3000)
    assert len(small) <= 3000
    for heading in ("【Key Facts】", "【Symbols / .symtab】", "【Relocations 逐筆】", "【字串】"):
        assert heading in small, heading
    assert "此段截斷" in small and "BIN_ELF_INGEST_MAX_CHARS" in small


@pytest.mark.smoke
def test_target_regex_is_guarded_against_redos(elf_path: Path):
    """審核 #5：target 直接 re.compile 後套到大量、可能很長的字串；災難性回溯會卡住同步的 MCP server。"""
    evil = "(a+)+$"
    t0 = time.monotonic()
    out = media.read_elf(str(elf_path), view="strings", target=evil)
    assert time.monotonic() - t0 < 5, "target regex 沒有 ReDoS 防護"
    assert "字面" in out, out                          # 明講改用字面比對，不是默默換掉


@pytest.mark.smoke
def test_section_dump_limit_matches_mcp_schema():
    """文件小錯：sections dump 上限寫 8192，但 analyze_file 的 limit schema 只允許到 5000。"""
    assert elf_analysis._SECTION_DUMP_MAX <= config.BIN_ELF_VIEW_MAX_LIMIT


# ---------------------------------------------------------------------------
# 2026-08-26 第三輪靜態審核回修
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_arm_vector_table_is_bounded(tmp_path: Path):
    """審核三 #1：ingest 把 10**9 當 limit 傳給 memmap，向量表會把整個 LOAD segment 讀成 IRQ。"""
    p = build_arm_exec_elf(tmp_path / "cm.elf", n_words=4000)
    media.set_sandbox_root(str(tmp_path), allow_external=False)
    elf_analysis._MODEL_CACHE.clear()
    model = elf_analysis.load_model(p)
    vt = elf_analysis.arm_vector_table(model, limit=10 ** 9)
    assert vt is not None
    assert len(vt["entries"]) <= elf_analysis._VECTOR_MAX_ENTRIES <= 512, len(vt["entries"])
    doc = media.read_binary_for_ingest(str(p))
    assert doc.count("IRQ") < 600, doc.count("IRQ")


@pytest.mark.smoke
def test_ingest_render_limits_are_bounded_by_cap(elf_path: Path, monkeypatch):
    """審核三 #2：400K 只裁最終字串；各 view 先以無上限完整 render，中間資料可到數十 MB。
    每個 view 的筆數上限必須從全域上限推出來（行預算），不能是 10**9。"""
    seen: list = []
    real_render = elf_analysis.render

    def spy(model, view="summary", target="", limit=0, **kw):
        seen.append((view, limit))
        return real_render(model, view, target, limit, **kw)

    monkeypatch.setattr(elf_analysis, "render", spy)
    elf_analysis.build_ingest_document(elf_path)
    assert seen
    line_budget = config.BIN_ELF_INGEST_MAX_CHARS // elf_analysis._INGEST_CHARS_PER_LINE
    for view, limit in seen:
        assert 0 < limit <= line_budget, (view, limit)


@pytest.mark.smoke
def test_dwarf_types_respect_type_cap(tmp_path: Path, monkeypatch):
    """審核三 #2：_DWARF_TYPE_CAP 定義了卻沒用；limit 很大時所有型別都會被建出來。"""
    gcc = shutil.which("gcc")
    if not gcc:
        pytest.skip("需要 gcc 產生帶 DWARF 的目標檔")
    src = tmp_path / "t.c"
    src.write_text(
        "struct a1 { int x; }; struct b2 { int y; }; struct c3 { int z; };\n"
        "int main(void) { struct a1 a = {1}; struct b2 b = {2}; struct c3 c = {3}; return a.x + b.y + c.z; }\n"
    )
    obj = tmp_path / "t.o"
    subprocess.run([gcc, "-g", "-O0", "-c", "-o", str(obj), str(src)], check=True, capture_output=True)
    elf_analysis._MODEL_CACHE.clear()
    model = elf_analysis.load_model(obj)
    monkeypatch.setattr(elf_analysis, "_DWARF_TYPE_CAP", 2)
    types, truncated = elf_analysis.dwarf_types(model, re.compile(""), 10 ** 9)
    assert len(types) <= 2 and truncated, (len(types), truncated)


@pytest.mark.smoke
def test_target_regex_rejects_optional_quantifier_bomb(elf_path: Path):
    """審核三 #3：heuristic 只數 + * {，一串 a?a?a?…a 的可選量詞炸彈照樣過關，然後同步 re.search 卡死。"""
    evil = "a?" * 25 + "a" * 25 + "X"
    t0 = time.monotonic()
    out = media.read_elf(str(elf_path), view="strings", target=evil)
    assert time.monotonic() - t0 < 5, "target regex 沒有擋住可選量詞炸彈"
    assert "字面" in out, out


@pytest.mark.smoke
def test_filter_deadline_is_checked_even_with_zero_matches(elf_path: Path, monkeypatch):
    """審核三 #3：deadline 只在「匹配數」到 2000 時檢查，零匹配的篩選永遠不會停。"""
    monkeypatch.setattr(elf_analysis, "_FILTER_TIME_BUDGET", 0.0)
    out = media.read_elf(str(elf_path), view="symbols", target="zzz_no_such_symbol_zzz")
    assert "時間預算" in out, out
    out = media.read_elf(str(elf_path), view="relocs", target="zzz_no_such_symbol_zzz")
    assert "時間預算" in out, out


@pytest.mark.smoke
def test_readelf_fallback_failure_propagates_to_memmap_sections_dwarf(elf_path: Path, monkeypatch):
    """審核三 #4：failed 只被 symbols / relocs / dynamic 消費；-lW / -SW 失敗後 memmap 仍說
    「沒有 LOAD」、sections 說「被 strip」、DWARF 說「沒有 debug section」——同一份輸出自相矛盾。"""
    if not shutil.which("readelf"):
        pytest.skip("binutils readelf 不存在，無法驗 fallback")
    monkeypatch.setattr(elf_analysis, "_HAS_PYELFTOOLS", False)
    real_run = elf_analysis.run_cmd

    def flaky(cmd, timeout=30):
        if cmd[0] == "readelf" and cmd[1] in ("-lW", "-SW"):
            return None, "timeout"
        return real_run(cmd, timeout)

    monkeypatch.setattr(elf_analysis, "run_cmd", flaky)
    elf_analysis._MODEL_CACHE.clear()
    media._ELF_CACHE.clear()
    # 斷言鎖的是「錯誤結論」那句原文；「讀取失敗（不能當成沒有…）」是正確的講法
    memmap = media.read_elf(str(elf_path), view="memmap")
    assert "REL 檔（.o / .ko）沒有 LOAD segment" not in memmap and "readelf -lW" in memmap, memmap
    sections = media.read_elf(str(elf_path), view="sections")
    assert "沒有 section header（可能被 strip 掉）" not in sections and "readelf -SW" in sections, sections
    dwarf = media.read_elf(str(elf_path), view="dwarf")
    assert "【DWARF】沒有 debug section" not in dwarf and "讀取失敗" in dwarf, dwarf
    summary = media.read_elf(str(elf_path))
    assert "DWARF    : absent" not in summary, summary


@pytest.mark.smoke
def test_bin_with_elf_magic_respects_max_chars(elf_path: Path, tmp_path: Path):
    """審核三 #5：.bin 內容是 ELF 時，[BIN→ELF] 前綴加在已截成 25K 的報告前，且 max_chars 沒轉傳。"""
    blob = tmp_path / "fw.bin"
    blob.write_bytes(elf_path.read_bytes())
    media._BIN_CACHE.clear()
    out = media.read_binary(str(blob), max_chars=3000)
    assert out.startswith("[BIN→ELF]") and len(out) <= 3000, len(out)
    out2 = media.read_binary(str(blob))
    assert len(out2) <= config.BIN_ELF_REPORT_MAX_CHARS, len(out2)


# ---------------------------------------------------------------------------
# 2026-08-26 第四輪靜態審核回修
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_target_regex_rejects_alternation_chain_bomb(elf_path: Path):
    """審核四 #1：正面表列只管量詞，30 個連續 (a|aa) 群組（零量詞、長度 < 200）照樣通過，
    對 a…a 再接不匹配字元就是指數回溯；單次 re.search 無法被 deadline 中斷。"""
    evil = "^" + "(a|aa)" * 30 + "$"
    t0 = time.monotonic()
    out = media.read_elf(str(elf_path), view="strings", target=evil)
    assert time.monotonic() - t0 < 5, "target regex 沒有擋住 alternation chain"
    assert "字面" in out, out
    # 最上層的 alternation 仍可用（各分支獨立、不會互相組合）
    ok = media.read_elf(str(elf_path), view="symbols", target="helper_static|main")
    assert "helper_static" in ok and "[注意]" not in ok, ok


@pytest.mark.smoke
def test_view_output_is_bounded_during_generation(elf_path: Path):
    """審核四 #2：hard_max 只是筆數，各 view 仍先完整產生再事後裁；渲染階段就要以字元預算截止，
    且輸出（含截斷說明）不得超過預算。"""
    elf_analysis._MODEL_CACHE.clear()
    model = elf_analysis.load_model(elf_path)
    for view, target in (("symbols", ""), ("strings", "cat:all"), ("relocs", "*"), ("sections", "")):
        full = elf_analysis.render(model, view, target, 0)          # 預設 25K 預算：這顆小 ELF 全部放得下
        out = elf_analysis.render(model, view, target, 0, char_budget=600)
        assert len(out) <= 600, (view, len(out))
        if len(full) > 600:                                          # 真的被預算切到的才必須有說明
            assert "字元" in out and "上限" in out, (view, out[-200:])
        else:
            assert out == full, view                                 # 放得下就一個字都不能少


@pytest.mark.smoke
def test_readelf_sections_only_failure_does_not_fake_memmap_stats(elf_path: Path, monkeypatch):
    """審核四 #3：只有 -SW 失敗時，memmap 用空的 sections 算出 code/rodata/data/bss 全 0。"""
    if not shutil.which("readelf"):
        pytest.skip("binutils readelf 不存在，無法驗 fallback")
    monkeypatch.setattr(elf_analysis, "_HAS_PYELFTOOLS", False)
    real_run = elf_analysis.run_cmd

    def flaky(cmd, timeout=30):
        if cmd[0] == "readelf" and cmd[1] == "-SW":
            return None, "timeout"
        return real_run(cmd, timeout)

    monkeypatch.setattr(elf_analysis, "run_cmd", flaky)
    elf_analysis._MODEL_CACHE.clear()
    media._ELF_CACHE.clear()
    memmap = media.read_elf(str(elf_path), view="memmap")
    assert "code 0 B" not in memmap, memmap
    assert "readelf -SW" in memmap, memmap


@pytest.mark.smoke
def test_bin_with_elf_magic_respects_tiny_max_chars(elf_path: Path, tmp_path: Path):
    """審核四 #4：body 至少 1,000 字元再加前綴，max_chars < ~1,050 時仍超限。"""
    blob = tmp_path / "fw.bin"
    blob.write_bytes(elf_path.read_bytes())
    media._BIN_CACHE.clear()
    for cap in (500, 300, 120):
        out = media.read_binary(str(blob), max_chars=cap)
        assert len(out) <= cap, (cap, len(out))
    assert len(elf_analysis.build_report(elf_path, "summary", max_chars=100)) <= 100


# ---------------------------------------------------------------------------
# 2026-08-26 第五輪靜態審核回修
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_headers_view_stops_generating_after_budget(tmp_path: Path, monkeypatch):
    """審核五 #1：headers 先用 _segments_table 把全部 program header 建成 list 才交給 _Sink，
    大量 segment 的 ELF 在預算生效前就先產生遠超預算的中間資料。渲染要在預算用完後停止產生。"""
    p = build_arm_exec_elf(tmp_path / "many.elf", n_words=64, n_null_phdrs=3000)
    media.set_sandbox_root(str(tmp_path), allow_external=False)
    elf_analysis._MODEL_CACHE.clear()
    model = elf_analysis.load_model(p)
    assert len(model.segments) == 3001
    calls = [0]
    orig_append = elf_analysis._Sink.append

    def counting_append(self, s):
        calls[0] += 1
        return orig_append(self, s)

    monkeypatch.setattr(elf_analysis._Sink, "append", counting_append)
    out = elf_analysis.render(model, "headers", "", 0, char_budget=1500)
    assert len(out) <= 1500
    kept = out.count("\n") + 1
    # 串流的定義：append 次數 ≈ 留下的行數 + 幾次被丟掉的嘗試；先建整份 list 會是幾千次
    assert calls[0] <= kept + 8, f"保留 {kept} 行卻 append 了 {calls[0]} 次（預算用完後仍在產生）"


@pytest.mark.smoke
def test_sink_does_not_truncate_reports_that_fit(elf_path: Path):
    """審核五 #2：_Sink 無條件預扣 200 字元，24,900 字元的報告在 25,000 上限下也會被截斷，
    違反「沒超過上限就是完整」。"""
    elf_analysis._MODEL_CACHE.clear()
    model = elf_analysis.load_model(elf_path)
    full = elf_analysis.render(model, "symbols", "", 0)
    exact = elf_analysis.render(model, "symbols", "", 0, char_budget=len(full))
    assert exact == full, "剛好放得下的報告不得被截斷"
    less = elf_analysis.render(model, "symbols", "", 0, char_budget=len(full) - 1)
    assert len(less) <= len(full) - 1 and "報告已截斷" in less, less[-200:]


@pytest.mark.smoke
def test_ingest_entry_cap_never_precedes_char_budget(tmp_path: Path):
    """審核五 #3：ingest 假設每行至少 20 字元（cap // 20 筆），imports 的短名稱列只有幾個字元，
    筆數上限會比字元預算先到，內容明明放得下卻被截斷。"""
    p = build_min_elf(tmp_path / "many_und.elf", extra_und=400)
    media.set_sandbox_root(str(tmp_path), allow_external=False)
    elf_analysis._MODEL_CACHE.clear()
    model = elf_analysis.load_model(p)
    cap = 6000
    n = elf_analysis._ingest_entry_cap(cap)
    out = elf_analysis.render(model, "imports", "", n, hard_max=n, char_budget=cap)
    assert len(out) <= cap
    assert "s399" in out and "... +" not in out, out[-300:]


# ---------------------------------------------------------------------------
# 2026-08-26 第六輪靜態審核回修
# ---------------------------------------------------------------------------

def _count_sink_appends(monkeypatch) -> list:
    calls = [0]
    orig_append = elf_analysis._Sink.append

    def counting_append(self, s):
        calls[0] += 1
        return orig_append(self, s)

    monkeypatch.setattr(elf_analysis._Sink, "append", counting_append)
    return calls


@pytest.mark.smoke
def test_symbols_candidate_heap_is_bounded_by_char_budget(elf_path: Path, monkeypatch):
    """審核六 #1：ingest 把筆數上限拉到 400K 並套給所有 view，symbols 的 heapq.nsmallest 會保留
    最多 400K 個候選——字元預算管不到。筆數上限要從「字元預算 ÷ 該 view 最短行長」推。"""
    seen: list = []
    real = elf_analysis.heapq.nsmallest

    def spy(n, it, key=None):
        seen.append(n)
        return real(n, it, key=key)

    monkeypatch.setattr(elf_analysis.heapq, "nsmallest", spy)
    elf_analysis._MODEL_CACHE.clear()
    model = elf_analysis.load_model(elf_path)
    cap = 2000
    n = elf_analysis._ingest_entry_cap(cap)
    elf_analysis.render(model, "symbols", "", n, hard_max=n, char_budget=cap)
    assert seen and max(seen) <= cap // 40 + 1, seen


@pytest.mark.smoke
def test_memmap_streams_to_sink(tmp_path: Path, monkeypatch):
    """審核六 #1：_memmap_lines 先完整建 list（3000 個 LOAD 全列）才交給 _Sink。"""
    p = build_arm_exec_elf(tmp_path / "loads.elf", n_words=64, n_load_phdrs=3000)
    media.set_sandbox_root(str(tmp_path), allow_external=False)
    elf_analysis._MODEL_CACHE.clear()
    model = elf_analysis.load_model(p)
    assert len(elf_analysis._load_segments(model)) == 3001
    calls = _count_sink_appends(monkeypatch)
    pulled = [0]
    real_iter = elf_analysis.iter_segment_kinds

    def counting_iter(m):
        for item in real_iter(m):
            pulled[0] += 1
            yield item

    monkeypatch.setattr(elf_analysis, "iter_segment_kinds", counting_iter)
    cap = 1500
    n = elf_analysis._ingest_entry_cap(cap)
    out = elf_analysis.render(model, "memmap", "", n, hard_max=n, char_budget=cap)
    assert len(out) <= cap
    kept = out.count("\n") + 1
    assert calls[0] <= kept + 8, f"保留 {kept} 行卻 append 了 {calls[0]} 次"
    # 審核七 #1：segment 分類也要是 lazy 的——不能為了前幾行先把 3001 個 LOAD 全部物化
    assert pulled[0] <= kept + 8, f"只留 {kept} 行卻物化了 {pulled[0]} 個 LOAD segment"


@pytest.mark.smoke
def test_strings_cat_all_stops_after_budget(tmp_path: Path, monkeypatch):
    """審核六 #2：ingest 用的 target="cat:all" 分支沒檢查 out.truncated，最多 100K 條字串會全部格式化。"""
    p = build_min_elf(tmp_path / "strs.elf", extra_strings=3000)
    media.set_sandbox_root(str(tmp_path), allow_external=False)
    elf_analysis._MODEL_CACHE.clear()
    model = elf_analysis.load_model(p)
    calls = _count_sink_appends(monkeypatch)
    cap = 1500
    n = elf_analysis._ingest_entry_cap(cap)
    out = elf_analysis.render(model, "strings", "cat:all", n, hard_max=n, char_budget=cap)
    assert len(out) <= cap
    kept = out.count("\n") + 1
    assert calls[0] <= kept + 8, f"保留 {kept} 行卻 append 了 {calls[0]} 次（預算用完後仍在產生）"


@pytest.mark.smoke
def test_manual_stop_reports_at_least(tmp_path: Path):
    """審核六 #3：relocs / memmap / DWARF / strings 在 out.truncated 時手動 break，省略了幾千行
    卻報「已略過 1 行」；提前停止的都只知道下限，要寫「至少」。"""
    p = build_min_elf(tmp_path / "relocs.elf", extra_relocs=2000)
    media.set_sandbox_root(str(tmp_path), allow_external=False)
    elf_analysis._MODEL_CACHE.clear()
    model = elf_analysis.load_model(p)
    cap = 1500
    n = elf_analysis._ingest_entry_cap(cap)
    out = elf_analysis.render(model, "relocs", "*", n, hard_max=n, char_budget=cap)
    assert len(out) <= cap
    assert "報告已截斷" in out and "至少" in out, out[-220:]


# ---------------------------------------------------------------------------
# 2026-08-26 第七輪靜態審核回修
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_min_line_chars_never_exceed_the_shortest_possible_line():
    """審核七 #3：筆數上限 = 字元預算 // 最短行長 + 1 的保證，前提是常數 ≤ 該 view 真的能印出的
    最短一行。DWARF 型別區塊有 `  enum X`（8 字元）和只有 6 個空白開頭的值列；relocs 最短是
    `  X+0x0  T  (none)`（18 字元）。"""
    m = elf_analysis._MIN_LINE_CHARS
    assert m["dwarf"] <= 6, m["dwarf"]
    assert m["relocs"] <= 18, m["relocs"]
    assert m["strings"] <= 19 and m["symbols"] <= 44 and m["imports"] <= 5
    assert m["sections"] <= 85 and m["dynamic"] <= 21 and m["memmap"] <= 37 and m["disasm"] <= 32

