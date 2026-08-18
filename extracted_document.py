#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文件結構原語:章節偵測、字元 span,以及 `ExtractedDocument` 這個單一真相。

為什麼要有這個模組
------------------
切 chunk 之後,「完整文件 + 章節範圍」在舊資料流裡無法無損還原:PDF 逐頁切、
四個入庫入口(document / chat / image / url)各自組 chunk dict,沒有任何一層
留下文件級座標。頁碼與章節因此有兩套來源——splitter 的 page-local 追蹤,與
呼叫端的 `last_section` 繼承——對不上時沒有仲裁者。

`ExtractedDocument` 就是那個仲裁者:每個入口先產出「正規化後的完整文字
(raw_text)+ 章節 span(sections)+ chunk(帶所屬 section 索引與字元 span)」,
下游才動。這裡只放不依賴設定 / 網路 / 檔案系統的純文字邏輯,讓 RAG.py
(抽取 + embedding + 儲存)與測試都能單獨 import。

座標約定
--------
所有 char offset 都相對於 `raw_text`,而 `raw_text` 是**正規化後**的文字
(`normalize_document_text()` 的輸出)——切 chunk 的人看到的就是這一份。
多頁文件的 `raw_text` 是各頁正規化文字以 `PAGE_SEPARATOR` 串接的結果,
`page_spans` 記錄每頁落在其中的範圍。

chunk 的 char span 是**來源行的範圍**,不是「content 的位元組還原」:content
會被加上 overlap / `[HEADING]` 合成前綴,表格續行還會補回表頭。它的用途是
定位(這個 chunk 落在哪一節、附近有什麼),不是重建原文。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# 多頁文件串接成 raw_text 時的頁間分隔。用空行而非單一換行,避免下一頁的
# 標題行黏在上一頁末行後面(會讓 is_heading 的行判斷失準)。
PAGE_SEPARATOR = "\n\n"

# Markdown 標題 pattern（# 開頭的行）
HEADING_PATTERN = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)


# ============================================================
# 標題 / 章節原語
# ============================================================
# 句尾標點：標題不會這樣結束，條列項與句子會。
_SENTENCE_TAIL = ("。", ".", "：", ":", "；", ";", "!", "?", "！", "？", ",", "，")
_MARKDOWN_HEADING = re.compile(r'^#{1,6}\s+')
_NUMBERED_HEADING = re.compile(r'^\d+\.[\d\.]*\s+[A-Z]')


def _has_cjk(text: str) -> bool:
    return any('\u4e00' <= ch <= '\u9fff' for ch in text)


def _looks_like_allcaps_heading(line: str) -> bool:
    """全大寫章節標題（常見於 PDF）。

    `str.isupper()` 對中英混排太寬:CJK 沒有大小寫,所以只要拉丁字母部分是大寫,
    整行就算 ALL CAPS。實測(三份真實 spec)這條規則命中 3 個標題,全是誤判——
    「PASS 畫面截圖如下：」「以下針對 NPX/VPX 測試項目逐一說明：」都中招。單獨
    一個詞（PASS）也不是標題,是測試結論。
    """
    if not (3 < len(line) < 100):
        return False
    if _has_cjk(line):
        return False
    if not line.isupper():
        return False
    if sum(1 for ch in line if ch.isalpha()) < 2:
        return False
    return len(line.split()) >= 2


def _looks_like_numbered_heading(line: str) -> bool:
    r"""數字章節標題 vs 編號條列項。

    實測(三份真實 spec):舊規則 `^\d+\.[\d\.]*\s+[A-Z]` 命中 73 次,**沒有一次**
    是真正的章節標題——真標題全部走 markdown 那條(`##` / `###`),這 73 個全是
    「2. Power on the HAPS system.」「1. L2 CPU selftest: DM, CSM, XM」這種條列項。
    它們變成 section 之後會污染 `[SECTION_METADATA]` 與 `[HEADING]` 兩個檢索訊號,
    而且會多切出一堆 chunk 邊界。

    規則本身不能刪:沒有 markdown 結構的純文字文件(「1. Introduction」)要靠它。
    所以改成兩段——多層編號(2.3 / 1.1.4)照舊放行;單層編號要「看起來像標題」:
    夠短、句中沒有冒號、句尾沒有標點。
    """
    if re.match(r'^\d+\.\d', line):
        return True
    if len(line) > 40:
        return False
    if line.endswith(_SENTENCE_TAIL):
        return False
    _, _, remainder = line.partition(".")
    return ":" not in remainder and "：" not in remainder


def is_heading(line: str) -> bool:
    """判斷是否為標題行"""
    line = line.strip()
    # Markdown 標題
    if _MARKDOWN_HEADING.match(line):
        return True
    # 全大寫行（常見於 PDF 章節標題）
    if _looks_like_allcaps_heading(line):
        return True
    # 數字開頭的章節（如 "1. Introduction", "2.3 Methods"）
    if _NUMBERED_HEADING.match(line) and _looks_like_numbered_heading(line):
        return True
    return False


def extract_section_title(line: str) -> str:
    """從標題行提取章節名稱

    判定條件與 `is_heading` 共用同一組 helper——兩邊各寫一套就會漂移，而漂移的
    症狀是「這行被當成標題、卻抽不出標題名」這種靜默的空 section。
    """
    line = line.strip()
    # Markdown 標題
    md_match = re.match(r'^#{1,6}\s+(.+)$', line)
    if md_match:
        return md_match.group(1).strip()
    # 數字章節
    num_match = re.match(r'^(\d+\.[\d\.]*\s+.+)$', line)
    if num_match and _looks_like_numbered_heading(line):
        return num_match.group(1).strip()
    # 全大寫標題
    if _looks_like_allcaps_heading(line):
        return line
    return ""


def heading_level(line: str) -> int:
    """標題層級（1 最高）。

    刻意保留既有的粗略規則（`####` 以下與數字章節一律算 2）:它同時餵
    `extract_heading_hierarchy`,而 hierarchy 會被烘進 chunk content——改動
    等於改動所有既存 KB 的 embedding 輸入。
    """
    line = line.strip()
    if line.startswith('### '):
        return 3
    if line.startswith('## '):
        return 2
    if line.startswith('# '):
        return 1
    if line.isupper():
        return 1
    return 2


def extract_heading_hierarchy(lines: list, current_idx: int) -> str:
    """提取章節標題層級

    返回格式：H1 > H2 > H3
    """
    hierarchy: List[Tuple[int, str]] = []

    for i in range(current_idx, -1, -1):
        line = lines[i].strip()
        if is_heading(line):
            title = extract_section_title(line)
            if title:
                level = heading_level(line)

                # 插入到正確位置
                while hierarchy and hierarchy[0][0] >= level:
                    hierarchy.pop(0)
                hierarchy.insert(0, (level, title))

    return ' > '.join(h[1] for h in hierarchy)


def normalize_table_content(text: str) -> str:
    """P1 改進：將表格/條列轉成 Key: Value 格式

    提升「最大值/預設值/限制」等問題的命中率
    """
    lines = text.split('\n')
    normalized = []

    for line in lines:
        # Markdown 表格行 (| col1 | col2 | col3 |)
        if '|' in line and line.count('|') >= 2:
            cells = [c.strip() for c in line.split('|') if c.strip()]
            if len(cells) >= 3:
                # Register maps need their original header + separator so the
                # splitter can repeat both when the table spans chunks.
                normalized.append(line)
            elif len(cells) >= 2 and not all(c == '-' or c.startswith('-') for c in cells):
                # 嘗試識別 key-value 對
                normalized.append(f"{cells[0]}: {cells[1]}")
            continue

        # 條列格式 (- key: value 或 * key: value)
        list_match = re.match(r'^[\-\*\•]\s*(.+?):\s*(.+)$', line.strip())
        if list_match:
            normalized.append(f"{list_match.group(1)}: {list_match.group(2)}")
            continue

        normalized.append(line)

    return '\n'.join(normalized)


def normalize_document_text(text: str) -> str:
    """切 chunk 前的正規化,單一定義。

    `raw_text` 與 chunk 的 char span 都以這個函式的輸出為座標系;呼叫端要自己
    先算一次 raw_text 時,必須走這裡,否則 offset 會對不上 splitter 實際切的
    那份文字。
    """
    return normalize_table_content(text.strip())


def build_line_offsets(text: str) -> List[int]:
    """每一行(以 '\\n' 切)在 text 中的起始 offset。"""
    offsets: List[int] = []
    position = 0
    for line in text.split('\n'):
        offsets.append(position)
        position += len(line) + 1
    return offsets


# ============================================================
# 資料結構
# ============================================================
@dataclass(frozen=True)
class Section:
    """文件的一節。

    char_span: [start, end) — 相對 `ExtractedDocument.raw_text`,含標題行本身。
    page_range: (first_page, last_page) — 無頁碼概念的來源一律 (1, 1)。
    """

    title: str
    level: int
    char_span: Tuple[int, int]
    page_range: Tuple[int, int] = (1, 1)


@dataclass
class ExtractedDocument:
    """一次抽取的完整結果:原文、章節、chunk。

    欄位順序刻意讓 `ExtractedDocument(raw_text, sections, chunks)` 可直接建構。
    `source` / `doc_type` / `page_spans` 是入口補上的識別資訊。
    """

    raw_text: str
    sections: List[Section] = field(default_factory=list)
    chunks: List[Dict] = field(default_factory=list)
    source: str = ""
    doc_type: str = "doc"
    # [(page_number, start, end), ...]，start/end 相對 raw_text
    page_spans: List[Tuple[int, int, int]] = field(default_factory=list)

    def section_index_for_offset(self, offset: int) -> int:
        """offset 落在第幾節;沒有任何 section 時回 -1。"""
        found = -1
        for index, section in enumerate(self.sections):
            start, end = section.char_span
            if start <= offset < end:
                return index
            if start <= offset:
                found = index
        return found

    def assign_section_indices(self) -> None:
        """替每個 chunk 標上所屬 section(以 chunk 起點決定)。

        用起點而非重疊面積:chunk 允許跨到下一節（overlap / 續行），但它的
        身分由「開始於哪一節」定義,與 splitter 追蹤 `section` 的規則一致。
        """
        for chunk in self.chunks:
            offset = int(chunk.get("char_start", 0) or 0)
            chunk["section_index"] = self.section_index_for_offset(offset)

    def apply_section_titles(self) -> int:
        """把每個 chunk 的 `section` 對齊文件級真相，回傳更正筆數。

        splitter 的 section 是 page-local 的：整頁塞得進一個 chunk 時它回空字串，
        舊 PDF 路徑就拿上一頁的 `last_section` 頂替——而那個值只在「某個 chunk 帶
        了非空 section」時才更新，於是連續幾頁短頁之後它會卡在過期章節上，讓開頭
        就是 `## 測試結果` 的 chunk 被歸到別節。實測（合成語料）：splitter 自己判定
        出非空 section 時，與文件級走訪 100% 一致；不一致的全是上述補值案例。

        只動 metadata：`[SECTION]` 前綴是在 splitter 裡烘進 content 的，那條路徑上
        兩者本來就一致，所以 content 位元組不變。
        """
        corrected = 0
        for chunk in self.chunks:
            try:
                index = int(chunk.get("section_index", -1))
            except (TypeError, ValueError):
                index = -1
            title = self.sections[index].title if 0 <= index < len(self.sections) else ""
            if chunk.get("section", "") != title:
                corrected += 1
            chunk["section"] = title
        return corrected

    def section_text(self, index: int) -> str:
        """第 index 節的原文(含標題行)。"""
        if index < 0 or index >= len(self.sections):
            return ""
        start, end = self.sections[index].char_span
        return self.raw_text[start:end]

    def page_for_offset(self, offset: int) -> Optional[int]:
        """offset 落在第幾頁;沒有頁碼資訊時回 None。"""
        for page, start, end in self.page_spans:
            if start <= offset < end:
                return page
        return None


# ============================================================
# 章節切分
# ============================================================
def page_range_for_span(
    span: Tuple[int, int], page_spans: List[Tuple[int, int, int]]
) -> Tuple[int, int]:
    """span 覆蓋到的頁碼範圍;沒有頁碼資訊時回 (1, 1)。"""
    if not page_spans:
        return (1, 1)
    start, end = span
    touched = [
        page for page, page_start, page_end in page_spans
        if page_start < end and start < page_end
    ]
    if not touched:
        return (1, 1)
    return (min(touched), max(touched))


def extract_sections(
    raw_text: str, page_spans: Optional[List[Tuple[int, int, int]]] = None
) -> List[Section]:
    """走一遍整份文字,切出章節 span。

    起始條件與 splitter 更新 `current_section` 的條件完全一致(`is_heading`
    為真**且** `extract_section_title` 非空),兩者才不會各自認定不同的章節。

    文件開頭在第一個標題之前的內容會成為一個 `title=""`、`level=0` 的前言節,
    好讓「每個 chunk 都屬於某一節」成立——否則每個消費點都得特判 -1。
    """
    page_spans = list(page_spans or [])
    if not raw_text:
        return []

    lines = raw_text.split('\n')
    offsets = build_line_offsets(raw_text)

    heads: List[Tuple[str, int, int]] = []  # (title, level, start_offset)
    for index, line in enumerate(lines):
        if not is_heading(line):
            continue
        title = extract_section_title(line)
        if not title:
            continue
        heads.append((title, heading_level(line), offsets[index]))

    sections: List[Section] = []
    first_head_offset = heads[0][2] if heads else len(raw_text)
    if first_head_offset > 0:
        span = (0, first_head_offset)
        sections.append(
            Section("", 0, span, page_range_for_span(span, page_spans))
        )

    for position, (title, level, start) in enumerate(heads):
        end = heads[position + 1][2] if position + 1 < len(heads) else len(raw_text)
        span = (start, end)
        sections.append(
            Section(title, level, span, page_range_for_span(span, page_spans))
        )

    return sections
