#!/usr/bin/env python3
"""
RAG 知識庫建立工具（增量模式）

用法：
    python3 RAG.py <input_file> <output_json>              # 一般文件（直接入庫）
    python3 RAG.py <screenshot> <output_json> --chat       # 聊天截圖（互動式）
    python3 RAG.py <image> <output_json> --image           # 技術圖片（互動式）
    python3 RAG.py <url> <output_json> --url               # 網頁（互動式）

範例：
    python3 RAG.py manual.pdf knowledge.json
    python3 RAG.py teams_chat.png knowledge.json --chat
    python3 RAG.py npx6_arch.png knowledge.json --image
    python3 RAG.py https://docs.example.com/guide knowledge.json --url
"""

import sys
import os
import re
import copy
import json
import hashlib
import contextlib
from dataclasses import replace as _dc_replace
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Tuple

from knowledge_store import (
    DocumentIdentityConflict,
    KnowledgeStoreError,
    check_document_identity,
    chunk_id,
    forget_document_identity,
    knowledge_store_lock,
    record_document_identity,
    save_knowledge_store_atomic,
    validate_embeddings,
)

# ============================================================
# 依賴檢查
# ============================================================
# 條件式依賴檢查，按模式載入
# - PDF 模式才需要 pymupdf4llm；PDF 有內嵌圖時另需 VL 端點（自動逐張入庫）
# - VL 模式（--chat/--image）需要 llama-server VL 端點 (預設 8083)
# - --url 模式需要 html2text
# - 所有模式都需要 llama-server embedding 端點 (預設 8081)

import context_signals
import ingest_notify
import kb_cache
import llama_client

# 執行期才讀的設定（旗標之類）走這個 module handle，不要 import-time 綁值；
# 獨立執行（沒有 config.py）時是 None，getattr 的預設值會接住。
try:
    import config as config_module
except ImportError:  # pragma: no cover - standalone 模式
    config_module = None


def check_pymupdf4llm():
    """檢查 pymupdf4llm 套件與釘版（只有 PDF 模式需要）

    釘版驗證走 config.require_pymupdf4llm()：版本不符會直接擋下，
    避免照著未釘版提示裝到最新版、頁碼靜默全錯（page→page_number schema 變動）。
    """
    try:
        from config import require_pymupdf4llm
    except ImportError as exc:
        # 釘版驗證是安全機制：config 缺失時 fail closed，不退回「只驗 import」
        # （那會讓任何版本被放行，與「所有 PDF 入口強制釘版」矛盾）。
        print(f"[ERROR] 無法載入 config.require_pymupdf4llm（釘版驗證必要）: {exc}")
        print("請在 CodeTrail repo 內執行（config.py 必須可 import）")
        sys.exit(1)
    try:
        return require_pymupdf4llm()
    except RuntimeError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)


# ============================================================
# 設定
# ============================================================
# 改進：從 config.py 統一匯入設定，避免兩處定義不一致
try:
    from config import (
        EMBEDDING_MODEL, CHUNK_SETTINGS,
        KNOWLEDGE_EMB_FILE,
        LLAMA_EMBED_BASE_URL, LLAMA_VL_BASE_URL,
        VL_MODEL, VL_INGEST_MAX_TOKENS, VL_INGEST_TIMEOUT,
    )
except ImportError:
    EMBEDDING_MODEL = "bge-m3"  # Fallback：獨立執行時的預設值
    KNOWLEDGE_EMB_FILE = "knowledge_emb.npz"
    CHUNK_SETTINGS = {'default': {'size': 1200, 'overlap': 200}}
    LLAMA_EMBED_BASE_URL = "http://localhost:8081"
    LLAMA_VL_BASE_URL = "http://localhost:8083"
    VL_MODEL = "qwen3.5-9b"
    VL_INGEST_MAX_TOKENS = 2048
    VL_INGEST_TIMEOUT = 300

# 預設 Chunk 設定（從 CHUNK_SETTINGS 取得）
CHUNK_SIZE = CHUNK_SETTINGS.get('default', {}).get('size', 1200)
CHUNK_OVERLAP = CHUNK_SETTINGS.get('default', {}).get('overlap', 200)
INCLUDE_HEADING_IN_CONTENT = True

# Embedding 增量快取檔案
EMBEDDING_CACHE_FILE = ".rag_embedding_cache.json"
# 組字與 schema 名稱的唯一定義在 context_signals；這裡只 re-export 舊名字。
EMBEDDING_INPUT_SCHEMA = context_signals.CONTENT_INPUT_SCHEMA
CONTEXTUAL_INPUT_SCHEMA = context_signals.CONTEXTUAL_INPUT_SCHEMA
GATE_INPUT_SCHEMA = context_signals.GATE_SCHEMA
LEGACY_CONTENT_HASH_SCHEMA = context_signals.LEGACY_CONTENT_HASH_SCHEMA

# 支援的檔案類型（文字類，process_file 走純文字抽取）
SUPPORTED_EXTENSIONS = {".pdf", ".md", ".txt"}

# 支援的圖片類型（聊天截圖模式）
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# 支援的二進位/ELF 副檔名（與 media.py 對齊；走 media.read_binary 抽報告）
BINARY_EXTENSIONS = {".bin", ".dat", ".raw", ".fw", ".img", ".rom", ".hex"}
ELF_EXTENSIONS = {".elf", ".so", ".o", ".axf", ".out", ".ko"}

# PDF native table 被 structured chunk 取代後，原位置留下的單行 marker（契約 §6.7 步驟 7）。
# 一定要是單行：它會被丟進既有的通用 splitter，多行會被當成段落而改變切點。
PDF_TABLE_REPLACED_MARKER = (
    "[表格已改以結構化 chunk 收錄：figure={figure_id} page={page} rows={rows}]"
)

# extract_pdf_document 掛在 ExtractedDocument 上的私有屬性（KB-aware prune 的資料通道）。
# 刻意用動態屬性而非 dataclass 欄位:`extracted_document.py` 不歸 T7 擁有,不加 schema 欄位。
_FIGURE_PRUNE_ATTR = "_codetrail_figure_prune"
# 同一條資料通道：這次 ingest **有內容、但沒有進 KB** 的頁與區域（頁碼 / bbox /
# 固定 slug，零文件內容）。缺席只在提交點的摘要裡講得清楚，抽取端是唯一知情的一端。
_ABSENT_ATTR = "_codetrail_absent_regions"

# ============================================================
# 文件類型識別
# ============================================================
DOC_TYPE_PATTERNS = {
    'spec': ['_spec', 'spec_', 'specification', '_datasheet', 'datasheet_'],
    'guide': ['_guide', 'guide_', 'tutorial', 'howto', 'how_to', 'quickstart'],
    'faq': ['faq', '_qa', 'q&a', 'questions'],
    'api': ['_api', 'api_', 'reference', '_ref'],
    'manual': ['manual', '_manual', 'handbook'],
}

# 內容特徵關鍵字（用於輔助文件類型識別）
CONTENT_TYPE_PATTERNS = {
    'spec': [
        r'(?i)specification',
        r'(?i)electrical\s+characteristics',
        r'(?i)absolute\s+maximum\s+ratings',
        r'(?i)timing\s+diagram',
        r'(?i)pin\s+configuration',
        r'(?i)register\s+map',
        r'(?i)typical\s+application',
    ],
    'api': [
        r'(?i)api\s+reference',
        r'(?i)endpoint[s]?\s*:',
        r'(?i)request\s+body',
        r'(?i)response\s+format',
        r'(?i)parameters?\s*:',
        r'(?i)returns?\s*:',
        r'def\s+\w+\s*\(',           # Python function def
        r'function\s+\w+\s*\(',       # JS function
    ],
    'guide': [
        r'(?i)getting\s+started',
        r'(?i)step\s+\d+',
        r'(?i)tutorial',
        r'(?i)example[s]?\s*:',
        r'(?i)how\s+to',
        r'(?i)quick\s*start',
    ],
    'faq': [
        r'(?i)frequently\s+asked',
        r'(?i)Q\s*:\s*',
        r'(?i)A\s*:\s*',
        r'(?i)問\s*[:：]',
        r'(?i)答\s*[:：]',
    ],
    'manual': [
        r'(?i)user\s+manual',
        r'(?i)操作\s*手冊',
        r'(?i)使用\s*說明',
        r'(?i)installation\s+guide',
        r'(?i)configuration',
    ],
}

# 警告/注意類內容的關鍵字
WARNING_KEYWORDS = [
    'WARNING', 'CAUTION', 'DANGER', 'NOTE:', 'IMPORTANT:',
    '警告', '注意', '危險', '請勿', '禁止', '不可', '切勿',
    '必須', 'MUST NOT', 'DO NOT', 'NEVER', '限制',
]


def detect_doc_type(filename: str) -> str:
    """根據檔名判斷文件類型"""
    name_lower = filename.lower()
    for doc_type, patterns in DOC_TYPE_PATTERNS.items():
        if any(p in name_lower for p in patterns):
            return doc_type
    return 'doc'  # 預設類型


def detect_doc_type_by_content(content: str, filename_type: str = 'doc') -> str:
    """根據內容特徵輔助判斷文件類型

    改進：當檔名無法識別類型時，使用內容特徵來判斷
    這可以提高 chunk 設定的準確性

    Args:
        content: 文件內容（前 2000 字元即可）
        filename_type: 從檔名推斷的類型（作為 fallback）

    Returns:
        文件類型
    """
    import re

    # 如果檔名已經識別出類型，直接返回
    if filename_type != 'doc':
        return filename_type

    # 只檢查前 2000 字元（效能考量）
    sample = content[:2000]

    # 計算各類型的匹配分數
    scores = {}
    for doc_type, patterns in CONTENT_TYPE_PATTERNS.items():
        score = 0
        for pattern in patterns:
            if re.search(pattern, sample):
                score += 1
        if score > 0:
            scores[doc_type] = score

    # 返回分數最高的類型，或 fallback 到 filename_type
    if scores:
        best_type = max(scores, key=scores.get)
        if scores[best_type] >= 2:  # 至少匹配 2 個特徵才認定
            return best_type

    return filename_type


def get_chunk_settings(doc_type: str) -> tuple:
    """根據文件類型取得 chunk 設定

    Args:
        doc_type: 文件類型（spec, api, manual, guide, faq, doc）

    Returns:
        (chunk_size, chunk_overlap) tuple
    """
    settings = CHUNK_SETTINGS.get(doc_type, CHUNK_SETTINGS.get('default', {}))
    size = settings.get('size', CHUNK_SIZE)
    overlap = settings.get('overlap', CHUNK_OVERLAP)
    return size, overlap


def detect_content_type(content: str, base_type: str) -> str:
    """
    根據內容判斷是否為警告類型
    如果內容包含警告關鍵字，覆蓋為 'warning' 類型

    例外：VL 產物（diagram/chat）不升級。「注意/必須/限制」在 VL 描述裡
    幾乎必然出現，一升級就把降權中的 VL chunk 拉到 warning 權重（1.15 >
    diagram 0.8），同時抹掉 type 層的出身標記。
    """
    if base_type in ('diagram', 'chat'):
        return base_type
    content_upper = content.upper()
    for kw in WARNING_KEYWORDS:
        if kw.upper() in content_upper:
            return 'warning'
    return base_type


# ============================================================
# 文字處理 - 語意切分
# ============================================================
# 標題偵測 / 表格正規化 / 章節層級都搬到 extracted_document.py（見該檔檔頭：
# 切完 chunk 後「完整文件 + 章節 span」必須能無損還原）。這裡 re-export 名字，
# 既有呼叫端（RAG.is_heading / RAG.normalize_table_content …）與測試不受影響。
from extracted_document import (  # noqa: E402  (在設定區塊之後才 import)
    HEADING_PATTERN,  # noqa: F401  (re-export，舊呼叫端仍讀 RAG.HEADING_PATTERN)
    PAGE_SEPARATOR,
    ExtractedDocument,
    Section,  # noqa: F401  (re-export)
    build_line_offsets,
    extract_heading_hierarchy,
    extract_section_title,
    extract_sections,
    heading_level,  # noqa: F401  (re-export)
    is_heading,
    normalize_document_text,
    normalize_table_content,  # noqa: F401  (re-export)
)


def split_by_semantic_with_sections(
    text: str,
    max_chars: int = CHUNK_SIZE,
    overlap_chars: int = CHUNK_OVERLAP,
    include_heading: bool = INCLUDE_HEADING_IN_CONTENT,
    pre_normalized: bool = False
) -> List[Dict]:
    """
    語意切分：按標題/段落切，保持語意完整性，同時追蹤章節標題

    P1 改進：
    - 表格/條列轉成 Key: Value 格式
    - 追蹤完整的標題層級

    char_start / char_end 是這個 chunk 取材的「來源行」範圍（相對切分時看到的
    正規化文字），給 ExtractedDocument 拿來定位章節與鄰近脈絡用；它**不是**
    content 的還原座標——content 另外會有 overlap / [HEADING] 合成前綴，表格
    續行還會補回表頭。

    pre_normalized=True 表示呼叫端已自己跑過 normalize_document_text()：多頁
    文件要先算出整份 raw_text 才能給出跨頁一致的 offset，這裡再正規化一次會
    讓座標對不上實際切出的文字。

    Returns: List[{content, section, heading_hierarchy, char_start, char_end}]
    """
    if not pre_normalized:
        # P1 改進：正規化表格內容（含前後空白修剪）
        text = normalize_document_text(text)
    if not text:
        return []

    if len(text) <= max_chars:
        return [{
            "content": text,
            "section": "",
            "heading_hierarchy": "",
            "char_start": 0,
            "char_end": len(text),
        }]

    lines = text.split('\n')
    line_offsets = build_line_offsets(text)
    table_context: Dict[int, Tuple[str, str]] = {}

    def _table_cells(line: str) -> List[str]:
        if '|' not in line or line.count('|') < 2:
            return []
        return [cell.strip() for cell in line.strip().strip('|').split('|')]

    def _is_table_separator(line: str) -> bool:
        cells = _table_cells(line)
        return len(cells) >= 3 and all(
            re.fullmatch(r':?-{3,}:?', cell or '') for cell in cells
        )

    # Map each data row to its table header.  This is computed before splitting
    # so a continuation chunk can restore column semantics deterministically.
    line_idx = 0
    while line_idx + 1 < len(lines):
        header_cells = _table_cells(lines[line_idx])
        if len(header_cells) >= 3 and _is_table_separator(lines[line_idx + 1]):
            header = lines[line_idx]
            separator = lines[line_idx + 1]
            data_idx = line_idx + 2
            while data_idx < len(lines) and len(_table_cells(lines[data_idx])) >= 3:
                table_context[data_idx] = (header, separator)
                data_idx += 1
            line_idx = data_idx
        else:
            line_idx += 1

    chunks = []
    current_chunk = []
    current_len = 0
    current_section = ""  # 追蹤當前章節
    chunk_start_idx = 0  # 用於計算 heading hierarchy

    def _span(start_idx: int, end_idx: int) -> Tuple[int, int]:
        """來源行 [start_idx, end_idx]（含）的字元範圍；前後空行不計入。"""
        start_idx = max(0, min(start_idx, len(lines) - 1))
        end_idx = max(start_idx, min(end_idx, len(lines) - 1))
        while start_idx < end_idx and not lines[start_idx].strip():
            start_idx += 1
        while end_idx > start_idx and not lines[end_idx].strip():
            end_idx -= 1
        return line_offsets[start_idx], line_offsets[end_idx] + len(lines[end_idx])

    def _emit(chunk_text: str, hierarchy_idx: int, start_idx: int, end_idx: int):
        """收下一個 chunk：內容與 section 照舊，另記來源行 span。"""
        char_start, char_end = _span(start_idx, end_idx)
        chunks.append({
            "content": chunk_text,
            "section": current_section,
            "heading_hierarchy": extract_heading_hierarchy(lines, hierarchy_idx),
            "char_start": char_start,
            "char_end": char_end,
        })

    for idx, line in enumerate(lines):
        line_len = len(line) + 1  # +1 for newline

        # 遇到標題 → 先 flush 舊 chunk（用舊 section），再更新 section
        if is_heading(line):
            # 先 flush 舊 chunk（保持舊的 section）
            if current_chunk:
                chunk_text = '\n'.join(current_chunk).strip()
                if chunk_text:
                    _emit(chunk_text, chunk_start_idx, chunk_start_idx, idx - 1)

            # 再更新 section
            section_title = extract_section_title(line)
            if section_title:
                current_section = section_title

            current_chunk = [line]
            current_len = line_len
            chunk_start_idx = idx
            continue

        # 空行 → 段落分界
        if not line.strip():
            if current_len > max_chars * 0.7:  # 超過 70% 就切
                chunk_text = '\n'.join(current_chunk).strip()
                if chunk_text:
                    _emit(chunk_text, chunk_start_idx, chunk_start_idx, idx - 1)
                current_chunk = []
                current_len = 0
                chunk_start_idx = idx + 1
            else:
                current_chunk.append(line)
                current_len += line_len
            continue

        # 加入當前行會超過限制 → 切分
        if current_len + line_len > max_chars:
            if current_chunk:
                chunk_text = '\n'.join(current_chunk).strip()
                if chunk_text:
                    _emit(chunk_text, chunk_start_idx, chunk_start_idx, idx - 1)

            # 單行超長 → 按句子切
            if line_len > max_chars:
                sub_chunks = split_long_paragraph(line, max_chars)
                for i, sc in enumerate(sub_chunks[:-1]):
                    _emit(sc, idx, idx, idx)
                current_chunk = [sub_chunks[-1]] if sub_chunks else []
                current_len = len(current_chunk[0]) if current_chunk else 0
            else:
                table_header = table_context.get(idx)
                if table_header:
                    current_chunk = [table_header[0], table_header[1], line]
                    current_len = sum(len(value) + 1 for value in current_chunk)
                else:
                    current_chunk = [line]
                    current_len = line_len
            chunk_start_idx = idx
        else:
            current_chunk.append(line)
            current_len += line_len

    # 處理最後的 chunk
    if current_chunk:
        chunk_text = '\n'.join(current_chunk).strip()
        if chunk_text:
            _emit(chunk_text, chunk_start_idx, chunk_start_idx, len(lines) - 1)

    chunks = [c for c in chunks if c["content"].strip()]
    for chunk in chunks:
        chunk["overlap_prefix_chars"] = 0
        chunk["heading_prefix_chars"] = 0

    # Add overlap for better recall.
    # 重要（P0 無損性）：overlap 是「附加在前面的前段脈絡」，必須是純前綴，
    # 絕不能為了壓在 max_chars 內而截斷「當前 chunk 的正文」——舊版
    #   curr = curr[:max_chars - len(tail)]
    # 會永久刪掉本段尾端（linker map 位址、表格末欄、限制條件），屬資料毀損。
    # chunk 允許因 overlap 而略超 max_chars（overlap+heading 合計 < embedding 上限）。
    # tail 一律取「原始」前段內容，避免 overlap 逐段累積污染。
    if overlap_chars and len(chunks) > 1:
        original_contents = [c["content"] for c in chunks]
        for i in range(1, len(chunks)):
            prev = original_contents[i - 1]
            tail = prev[-overlap_chars:] if prev else ""
            if tail:
                prefix = tail + "\n"
                chunks[i]["content"] = prefix + chunks[i]["content"]
                chunks[i]["overlap_prefix_chars"] = len(prefix)

    # Inject heading hierarchy into content to improve retrieval.
    # 同為純前綴，不截斷正文（舊版 content[:max_chars]+"..." 同樣會遺失原文）。
    if include_heading:
        for chunk in chunks:
            header_lines = []
            if chunk.get("heading_hierarchy"):
                header_lines.append(f"[HEADING] {chunk['heading_hierarchy']}")
            if chunk.get("section"):
                header_lines.append(f"[SECTION] {chunk['section']}")
            if header_lines:
                prefix = "\n".join(header_lines) + "\n"
                chunk["content"] = prefix + chunk["content"]
                chunk["heading_prefix_chars"] = len(prefix)

    return chunks


def split_by_semantic(text: str, max_chars: int = CHUNK_SIZE) -> List[str]:
    """
    語意切分：按標題/段落切，保持語意完整性
    （向後相容的簡化版本）
    """
    results = split_by_semantic_with_sections(text, max_chars)
    return [r["content"] for r in results]


def _retrieval_prefix_metadata(chunk_data: Dict) -> Dict[str, int]:
    """Carry synthetic-prefix boundaries from the splitter into stored chunks."""
    return {
        "overlap_prefix_chars": int(chunk_data.get("overlap_prefix_chars", 0) or 0),
        "heading_prefix_chars": int(chunk_data.get("heading_prefix_chars", 0) or 0),
    }

def _chunk_locator_metadata(chunk_data: Dict, offset: int = 0) -> Dict[str, int]:
    """Carry the splitter's source-line span into stored chunks.

    `offset` 是這一頁在文件 raw_text 裡的起點——PDF 逐頁切，chunk 的 span 要
    加上頁位移才會落在文件座標系上。
    """
    return {
        "char_start": int(chunk_data.get("char_start", 0) or 0) + offset,
        "char_end": int(chunk_data.get("char_end", 0) or 0) + offset,
    }

def split_long_paragraph(text: str, max_chars: int) -> List[str]:
    """切分超長段落（按句子）"""
    # 句子分隔符
    sentences = re.split(r'(?<=[.。!?！？])\s+', text)

    chunks = []
    current = ""

    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue

        if len(current) + len(sent) + 1 <= max_chars:
            current = current + " " + sent if current else sent
        else:
            if current:
                chunks.append(current)
            # 單句超長 → 強制切
            if len(sent) > max_chars:
                for i in range(0, len(sent), max_chars):
                    chunks.append(sent[i:i+max_chars])
                current = ""
            else:
                current = sent

    if current:
        chunks.append(current)

    return chunks

# 兼容舊 API
def split_text(text: str, max_chars: int = CHUNK_SIZE) -> List[str]:
    """將文字分割成適當大小的 chunks（使用語意切分）"""
    return split_by_semantic(text, max_chars)

# ============================================================
# 檔案處理
# ============================================================
def build_text_document(
    content: str,
    *,
    source: str,
    base_type: str,
    doc_type: str = "doc",
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
    page: int = 1,
    extra: Optional[Dict] = None,
) -> ExtractedDocument:
    """單一文字來源（md/txt、VL 抽述、網頁、binary 報告）→ ExtractedDocument。

    四個入庫入口以前各自複製這段組 chunk dict 的程式碼，欄位一有出入就是靜默
    的 retrieval 差異（少一個 *_prefix_chars 就會讓 BM25 重複計算前綴）。共用
    之後 chunk 的形狀只有一處定義。

    `base_type` 餵 detect_content_type（可升級成 warning）；`doc_type` 只是
    文件層標記，不影響 chunk。
    """
    raw_text = normalize_document_text(content)
    chunk_results = split_by_semantic_with_sections(
        raw_text,
        max_chars=chunk_size,
        overlap_chars=chunk_overlap,
        pre_normalized=True,
    )

    chunks: List[Dict] = []
    for i, chunk_data in enumerate(chunk_results):
        chunk = {
            "source": source,
            "page": page,
            "chunk_index": i,
            "content": chunk_data["content"],
            "type": detect_content_type(chunk_data["content"], base_type),
            "section": chunk_data["section"],
            "heading_hierarchy": chunk_data.get("heading_hierarchy", ""),
            **_retrieval_prefix_metadata(chunk_data),
            **_chunk_locator_metadata(chunk_data),
        }
        if extra:
            chunk.update(extra)
        chunks.append(chunk)

    document = ExtractedDocument(
        raw_text=raw_text,
        sections=extract_sections(raw_text),
        chunks=chunks,
        source=source,
        doc_type=doc_type,
        page_spans=[(page, 0, len(raw_text))] if raw_text else [],
    )
    document.assign_section_indices()
    document.apply_section_titles()
    return document


# ============================================================
# PDF 結構化 figure lane（table / terminal / prose / diagram）
# ============================================================
# PDF 的圖面只有這一條 lane（契約 §0.1 / §13.1；2026-08-30 移除 legacy 自由文字 lane）：
# 有結構性原生證據的候選，以及夠大的純 raster / picture 候選，後者先用 image-bound
# schema 分成 table / terminal / prose / diagram（或「不是圖面」而跳過），再產生
# canonical JSON payload → origin="figure_table" / "figure_terminal" / "figure_prose"
# / "figure_diagram"。
#
# schema / validator 失敗一律整份 PDF 零寫入（workflow §7「不保留自由文字 fallback」）；
# lane 沒收的頁與區域是**缺席**，由 `format_absent_regions()` 與 ingest 摘要列出頁碼、
# bbox 與原因。既有 KB 裡的 `origin="diagram"` chunk 仍保留原語意（legacy_unverified）。
class PdfPreflightUnavailable(RuntimeError):
    """`--preflight` 無法產出預算報告（PDF 解析失敗、或結構化 lane 未啟動）。

    存在的理由：契約 §11.4 把 `--preflight` 的 exit code 凍結成 0/2/1，而
    「沒有報告卻回 0」是假成功——使用者會以為預算沒問題就直接開跑。CLI 把這個
    例外映射成 exit 1。
    """


def _figure_extract():
    """結構化 figure lane 的唯一門面（延遲載入：只有 PDF 模式需要）。

    一律 `import figure_extract` 後走 module attribute 取用，不用
    `from figure_extract import x`——那會在 import 時把函式快照進本模組，測試
    monkeypatch 門面就打不到（同 AGENTS.md §3 對 config 的規定）。
    """
    import figure_extract
    return figure_extract


def _figure_root(root: Optional[str]) -> Path:
    """figure lane 的專案根：明示 root > `AICODE_ROOT` > cwd。

    明示 root 與 `AICODE_ROOT` 不一致時**立刻** fail-loud：`figure_review._resolve_root`
    要求兩者解析後完全相同，若拖到寫 artifact 才失敗，中間已經呼叫過 VL、算過
    embedding（契約 §6.5 / §12.3）。`root=None` 時取的就是 `AICODE_ROOT` 自己，
    不可能衝突，所以這條只會打到「呼叫端明確傳了不同 root」的程式錯誤。
    """
    env_text = os.environ.get("AICODE_ROOT", "").strip()
    env_real = Path(env_text).resolve() if env_text else None
    if root is None or not str(root).strip():
        return env_real or Path.cwd().resolve()
    explicit = Path(str(root)).resolve()
    if env_real is not None and env_real != explicit:
        raise _figure_extract().FigureReviewError(
            f"root {explicit} 不是目前的 AICODE_ROOT {env_real}。"
            "review artifacts 只能寫在 sandbox root 內（契約 §6.5）；"
            "在呼叫任何 VL / embedding 之前就停下。"
        )
    return explicit


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _bbox_iou(a, b) -> float:
    """兩個 (x0, y0, x1, y1) 的 IoU；退化框回 0.0。"""
    try:
        ax0, ay0, ax1, ay1 = (float(v) for v in a)
        bx0, by0, bx1, by1 = (float(v) for v in b)
    except (TypeError, ValueError):
        return 0.0
    iw = min(ax1, bx1) - max(ax0, bx0)
    ih = min(ay1, by1) - max(ay0, by0)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _iou_threshold() -> float:
    return float(getattr(config_module, "FIGURE_IOU_MERGE", 0.5))


# figure caption 的辨識（只認**行首**的圖表編號；行中間的「見 Table 3-1」是引用不是題名）。
# 兩個 pattern 分開是刻意的：中英文的分隔慣例不同（`Table 3-1. Foo` vs `表 3-1 Foo`），
# 合成一條會讓其中一邊的編號吃進題名。
_CAPTION_PATTERNS = (
    ("table", re.compile(
        r"^\s{0,3}(?:\*\*|__)?\s*"
        r"(?P<label>(?:Table|TABLE|Tab\.)\s*[0-9A-Za-z][0-9A-Za-z.\-‐-―]*)"
        r"\s*[.:。：\-–—]?\s+(?P<title>\S.*?)\s*(?:\*\*|__)?\s*$")),
    ("figure", re.compile(
        r"^\s{0,3}(?:\*\*|__)?\s*"
        r"(?P<label>(?:Figure|FIGURE|Fig\.|FIG\.)\s*[0-9A-Za-z][0-9A-Za-z.\-‐-―]*)"
        r"\s*[.:。：\-–—]?\s+(?P<title>\S.*?)\s*(?:\*\*|__)?\s*$")),
    ("table", re.compile(
        r"^\s{0,3}(?P<label>(?:表|附表)\s*[0-9][0-9.\-‐-―]*)"
        r"[\s、:：]*(?P<title>\S.*?)\s*$")),
    ("figure", re.compile(
        r"^\s{0,3}(?P<label>(?:圖|图|附圖)\s*[0-9][0-9.\-‐-―]*)"
        r"[\s、:：]*(?P<title>\S.*?)\s*$")),
)
# caption 一行最多這麼長；超過就不是題名，是把整段內文誤認成 caption。
MAX_CAPTION_CHARS = 200


def _page_captions(page_text: str, page_start: int) -> List[Dict]:
    """一頁文字裡的 caption 候選（依出現順序）。

    只認行首的圖表編號：datasheet 的內文到處都是「見 Table 3-1」，把那些也算進來
    的話，一頁的 caption 數會遠多於圖數，配對就只能整頁放棄。
    """
    found: List[Dict] = []
    offset = 0
    for line in page_text.split("\n"):
        stripped = line.strip()
        if 0 < len(stripped) <= MAX_CAPTION_CHARS:
            for family, pattern in _CAPTION_PATTERNS:
                match = pattern.match(line)
                if match:
                    found.append({
                        "family": family,
                        "offset": page_start + offset,
                        "text": f"{match.group('label')} {match.group('title')}".strip(),
                    })
                    break
        offset += len(line) + 1        # +1 = 被 split 掉的那個 "\n"
    return found


def _caption_family(kind: str) -> str:
    """figure kind → caption 家族。table 找「Table N」，其餘找「Figure N」。"""
    return "table" if kind == "table" else "figure"


def _figure_retrieval_context(document, figures) -> Dict[str, Dict[str, str]]:
    """每張 structured figure 的 caption 與所在章節（**只**當檢索訊號）。

    caption 與 figure 的配對規則刻意保守：同一頁、同一個 caption 家族的數量**完全
    相等**時才依閱讀順序一一配對，否則整頁放棄。掛錯 caption 比沒有 caption 更糟
    ——「Table 3-1 的 opcode」會穩定命中另一張表，而且看起來完全正常。

    章節取 caption 所在位置的那一節；沒有 caption 就退回該頁起點。兩者都是文件級
    走訪的結果（`ExtractedDocument.section_index_for_offset`），與文字 chunk 同一套
    座標，不會出現「同一頁的文字 chunk 說第 3.2 節、figure 說空字串」。
    """
    spans = {page: (start, end) for page, start, end in (document.page_spans or [])}
    raw = document.raw_text or ""
    # offset → 文字 chunk 的 heading_hierarchy（figure 沿用它所在位置那一段的階層）
    text_chunks = sorted(
        (c for c in document.chunks if not c.get("structured")),
        key=lambda c: int(c.get("char_start", 0) or 0),
    )

    def _heading_at(offset: int) -> str:
        found = ""
        for chunk in text_chunks:
            if int(chunk.get("char_start", 0) or 0) > offset:
                break
            if int(chunk.get("char_end", 0) or 0) > offset:
                found = str(chunk.get("heading_hierarchy", "") or "")
        return found

    def _section_at(offset: int) -> str:
        index = document.section_index_for_offset(offset)
        sections = document.sections or []
        return sections[index].title if 0 <= index < len(sections) else ""

    by_page: Dict[int, List] = {}
    for figure in figures:
        by_page.setdefault(int(figure.page), []).append(figure)

    def _anchor_offset(page: int) -> Optional[int]:
        """這一頁在 raw_text 的定位點；證明不出來就回 None（章節因此留空）。

        `page_spans` 只收「產出過文字」的頁，所以純圖片頁查不到 span。退回 offset 0
        會讓那一頁所有 figure 都被標成**文件開頭那一節**。沿用前一個文字頁的章節也
        不行：掃描頁完全可能在**影像裡**開新的一節，只是沒有文字層——那只是把「錯掛
        第一節」換成「可能錯掛前一節」，而錯掛的檢索訊號看起來與正確的一模一樣。
        證明不出來就不宣稱。
        """
        span = spans.get(page)
        return span[0] if span else None

    context: Dict[str, Dict[str, str]] = {}
    for page, page_figures in by_page.items():
        span = spans.get(page)
        anchor = _anchor_offset(page)
        captions = _page_captions(raw[span[0]:span[1]], span[0]) if span else []
        page_figures.sort(key=lambda f: (float(f.bbox[1]), float(f.bbox[0])))
        for family in ("table", "figure"):
            group = [f for f in page_figures if _caption_family(str(f.kind)) == family]
            available = [c for c in captions if c["family"] == family]
            if not group or len(group) != len(available):
                continue
            for figure, caption in zip(group, available):
                context[figure.figure_id] = {
                    "caption": caption["text"],
                    "section": _section_at(caption["offset"]),
                    "heading_hierarchy": _heading_at(caption["offset"]),
                }
        for figure in page_figures:
            context.setdefault(figure.figure_id, {
                "caption": "",
                "section": _section_at(anchor) if anchor is not None else "",
                "heading_hierarchy": _heading_at(anchor) if anchor is not None else "",
            })
    return context


def build_structured_figure_document(
    figures,
    *,
    source: str,
    doc_type: str,
    next_chunk_index: Dict[int, int],
    evidence_ref_by_figure: Dict[str, str],
    context_by_figure: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[Dict]:
    """structured figure chunk 的唯一入口：薄封裝 `figure_extract.build_figure_chunks`。

    **刻意什麼都不做**。structured chunk 的 content 是 canonical payload 的衍生
    文字（JSON 才是真相），一旦經過 `normalize_document_text()` /
    `normalize_table_content()` / `split_by_semantic*`，兩欄表會被改寫成
    `Key: Value`、terminal 的首尾空行與行首空白會被 strip 掉——那是 workflow §8
    第 2 條「原文」的直接違反。通用語意切分本身不改（契約 §6.7）。

    `next_chunk_index` 由 `build_figure_chunks` 就地更新：整批成功才提交，中途失敗時
    呼叫端的 dict 完全不變。`context_by_figure` 是 caption / 章節這類**檢索訊號**，
    同樣原樣轉傳（它們不進 content，見 `build_figure_chunks` 的說明）。
    """
    return _figure_extract().build_figure_chunks(
        figures,
        source=source,
        doc_type=doc_type,
        next_chunk_index=next_chunk_index,
        evidence_ref_by_figure=evidence_ref_by_figure,
        context_by_figure=context_by_figure,
    )


def _first_page_texts(pages: List[Dict]) -> Dict[int, str]:
    """頁碼 → 該頁 raw markdown（`strip`/normalize 之前）。

    只取**第一個**宣稱該頁碼的 page dict：畸形 metadata 會讓兩個 dict 撞同一頁，
    把 A 的 `pos` 套到 B 的文字上就是在正文中間亂切。
    """
    texts: Dict[int, str] = {}
    for page_info in pages:
        page_num = _pdf_page_number(page_info.get('metadata'))
        texts.setdefault(page_num, page_info.get('text', '') or '')
    return texts


def _native_box_pos(evidence, bbox, threshold: float, classes):
    """在該頁 `page_boxes` 找出覆蓋 `bbox` 的原文框，回傳它的 `pos`。

    這是「候選 identity 核對」：`pos` 必須來自一個真的被上游標成該類別的框，
    否則我們是拿一段不知道是什麼的區間去刪正文。

    `classes` 依來源決定（table 只認 `table`，`native_text` 認 T3 的文字類）。
    key 名同時吃上游原始的 `pos`/`bbox` 與 T3 正規化後的 `_pos`/`_bbox_unrotated`
    ——只認其中一組的話，在真 planner 的 evidence 上這道核對會靜默失效。
    """
    boxes = getattr(evidence, "page_boxes", None) or []
    allowed = set(classes or ())
    best = None
    best_iou = 0.0
    for box in boxes:
        if not isinstance(box, dict) or box.get('class') not in allowed:
            continue
        raw_pos = box.get('_pos') if box.get('_pos') is not None else box.get('pos')
        if raw_pos is None:
            continue
        box_bbox = box.get('_bbox_unrotated') or box.get('bbox') or ()
        iou = _bbox_iou(box_bbox, bbox)
        if iou >= threshold and iou > best_iou:
            best, best_iou = raw_pos, iou
    return best


def _normalize_pos(raw_pos):
    """`pos` → `(start, end)`；型別不對（float / bool / 長度不符）一律回 None。"""
    if raw_pos is None:
        return None
    try:
        values = list(raw_pos)
    except TypeError:
        return None
    if len(values) != 2:
        return None
    out = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        out.append(value)
    return (out[0], out[1])


def _table_pos_problem(fx, raw: str, raw_pos, native_markdown: str):
    """`pos` 能不能安全地把那段 markdown 表切掉？不能就回中文原因。

    六種判定共用 reason slug `no_pos_cannot_replace`（契約 §13.4），細節寫進
    `reason_details`。**光是 in-range 還不夠**：一個合法但指錯地方的區間會靜默
    刪掉一段正文，所以最後還要證明區間內容真的就是那張表。
    """
    pos = _normalize_pos(raw_pos)
    if pos is None:
        return "缺 pos"
    start, end = pos
    if not (0 <= start < end <= len(raw)):
        return f"pos 越界（pos={(start, end)}, 頁文字長度={len(raw)}）"
    slice_text = raw[start:end]
    if not slice_text.strip():
        return f"pos 指向空白區間（pos={(start, end)}）"
    expected = (native_markdown or "").strip()
    if not expected:
        return "候選缺 native markdown，無法證明 pos 指向那張表"
    if expected not in slice_text:
        return "pos 指向的文字不含候選的 native markdown"
    if fx.normalize_for_compare(slice_text) != fx.normalize_for_compare(expected):
        return "pos 區間比候選的 native markdown 多出其他正文"
    return None


def _overlap_groups(items):
    """把 `(start, end, ...)` 依重疊關係分群（connected component + running max end）。

    只比相鄰區間會漏掉巢狀：A=[0,100)、B=[10,20)、C=[30,40) 之中 C 與 B 不相鄰，
    但兩者都被 A 蓋住。維護 running maximum end 才抓得到整個 component。
    """
    groups = []
    current = []
    current_end = None
    for item in sorted(items, key=lambda entry: (entry[0], entry[1])):
        start, end = item[0], item[1]
        if current and start < current_end:
            current.append(item)
            current_end = max(current_end, end)
            continue
        if current:
            groups.append(current)
        current = [item]
        current_end = end
    if current:
        groups.append(current)
    return groups


def _marker_piece(raw: str, start: int, end: int, text: str) -> str:
    """marker 的換行守衛：保證它獨佔一行，且只在必要時多加位元組。

    實測 pymupdf4llm 1.28.0：`raw[start-1]` 通常是 '\\n'（行首對齊），但 `raw[end]`
    可能是正文首字（table box 的 pos 已經把尾端空行吃掉了）。
    """
    piece = text
    if start > 0 and raw[start - 1] != "\n":
        piece = "\n" + piece
    if end < len(raw) and raw[end] != "\n":
        piece = piece + "\n"
    return piece


def _apply_page_replacements(raw: str, pieces) -> str:
    """cursor 掃描法：從頭不修改 `raw`，依 `start` 遞增讀切片。

    **不得**改成就地字串替換：所有 `pos` 都是相對原始 `raw` 的座標，替換第一段
    之後（marker 長度 ≠ 表格長度）後面每個 offset 都被整體位移。真要就地替換必須
    改成依 `start` 遞減處理；cursor 法沒有這個陷阱。
    """
    out = []
    cursor = 0
    for start, end, piece in sorted(pieces, key=lambda entry: entry[0]):
        out.append(raw[cursor:start])
        out.append(piece)
        cursor = end
    out.append(raw[cursor:])
    return "".join(out)


def _native_span(fx, candidate) -> Optional[Dict]:
    """候選的正文是不是**已經在 page markdown 裡**？是的話回傳 `{pos, markdown, classes}`。

    兩個來源，形狀相同（都帶 `pos` 與 `markdown`）：

    - `candidate.native_table`：原生表格（`class=table` 的框）。
    - `candidate.signals["native_text"]`：`pos` 支撐的 raw markdown 文字，terminal 的
      native 正文就是它（契約 §15.1）。**漏掉這一條**就會讓原始 log 與 structured
      chunk 同時留在 KB——同一份內容兩個互相競爭的版本，違反 workflow §8-4。

    `classes` 是允許用來做 identity 核對的 page box class：table 只認 `table`，
    terminal 認 T3 定義的文字類（`_native_text_span` 就是從這些框挑出來的）。
    """
    native_table = getattr(candidate, "native_table", None)
    if isinstance(native_table, dict) and native_table.get("pos") is not None:
        return {"pos": native_table.get("pos"),
                "markdown": native_table.get("markdown"),
                "classes": ("table",), "kind": "native_table"}
    if isinstance(native_table, dict):
        # 有 native_table 但沒有 pos：仍然要走「不能安全取代」那條，不能當成沒有正文
        return {"pos": None, "markdown": native_table.get("markdown"),
                "classes": ("table",), "kind": "native_table"}
    signals = getattr(candidate, "signals", None) or {}
    native_text = signals.get("native_text")
    if isinstance(native_text, dict):
        return {"pos": native_text.get("pos"),
                "markdown": native_text.get("markdown"),
                "classes": ("text", "code", "table", "section-header"),
                "kind": "native_text"}
    return None


def _structured_candidates(fx, plan) -> Tuple[List, List[Dict]]:
    """哪些候選進 structured lane；回 `(keep, absent)`。

    `KIND_RASTER` 是 candidate-only：必須先由 VL 的受限分類 schema 解成 table /
    terminal / diagram / prose，結果才可入庫。

    structured lane 是**唯一**的圖面 lane（2026-08-30），所以這裡濾掉的候選就是
    「不會進 KB」——每一個都要留下頁碼、bbox 與原因，否則它就是無聲缺席。
    """
    keep, absent = [], []
    accepted = (fx.KIND_TABLE, fx.KIND_TERMINAL, fx.KIND_UNKNOWN, fx.KIND_RASTER)
    for candidate in plan.candidates:
        kind = getattr(candidate, "kind", "")
        if kind != fx.KIND_DIAGRAM and (
                getattr(candidate, "native_table", None) is not None or kind in accepted):
            keep.append(candidate)
            continue
        absent.append({
            "page": int(getattr(candidate, "page", 0) or 0),
            "bbox": [float(v) for v in (getattr(candidate, "bbox", ()) or ())] or None,
            "channel": "candidate",
            "reason": f"candidate_kind_not_supported:{kind or 'unknown'}",
        })
    return keep, absent


def _pdf_picture_pages(pages: List[Dict]) -> List[int]:
    """哪些頁的上游 page dict 宣稱有內嵌圖（`class=picture`，舊 schema 是 `images`）。

    structured lane 沒啟動時（stub 路徑、檔案不在專案根內）唯一的圖面線索。沒有它
    就分不出「這份 PDF 本來就沒有圖」與「有圖、但整條 lane 一次都沒跑」——前者不該
    吵，後者不說就是無聲缺席。
    """
    found: List[int] = []
    for page_info in pages or []:
        if not isinstance(page_info, dict):
            continue
        has_picture = any(
            isinstance(box, dict) and box.get("class") == "picture"
            for box in page_info.get("page_boxes") or ()
        ) or bool(page_info.get("images"))
        if has_picture:
            found.append(_pdf_page_number(page_info.get("metadata")))
    return found


def _absent_from_plan(plan, extra: List[Dict]) -> List[Dict]:
    """planner 算出來的缺席清單 ＋ 呼叫端自己濾掉的候選（形狀一致、順序穩定）。"""
    entries = [dict(item) for item in (getattr(plan, "stats", None) or {}).get(
        "absent_regions", ())]
    entries.extend(dict(item) for item in extra)
    entries.sort(key=lambda e: (int(e.get("page") or 0),
                                (e.get("bbox") or [0.0, 0.0])[1],
                                (e.get("bbox") or [0.0, 0.0])[0],
                                str(e.get("reason") or "")))
    return entries


# 缺席清單在 stdout 逐筆列出的上限（超過補一行「…還有 N 筆」）。
MAX_LISTED_ABSENT = 10


def format_absent_regions(filename: str, absent: List[Dict]) -> str:
    """「這一次有東西沒進 KB」的人類可讀區塊；沒有就回空字串。

    ingest 仍然 exit 0，使用者只從 chunk 數看不出少了什麼；而缺席的內容在查詢時
    是**完全不存在**的，不說出來就會變成「問了得到查無資料、以為文件裡沒寫」。
    只列頁碼、bbox 與固定 slug——一個字的文件內容都不含（NDA）。
    """
    if not absent:
        return ""
    lines = [f"[INFO] {filename}: {len(absent)} 個頁 / 區域沒有進知識庫"
             "（查詢時不會出現，需要就用 read_pdf 直接看原頁）"]
    for entry in absent[:MAX_LISTED_ABSENT]:
        bbox = entry.get("bbox")
        where = f"bbox={[round(float(v), 1) for v in bbox]}" if bbox else "整頁"
        page = int(entry.get("page") or 0)
        location = f"第 {page} 頁" if page else "整份文件"
        lines.append(f"  - {location} {where} reason={entry.get('reason', '')}")
    remaining = len(absent) - min(len(absent), MAX_LISTED_ABSENT)
    if remaining > 0:
        lines.append(f"  …還有 {remaining} 筆（完整清單見 preflight 報告）")
    return "\n".join(lines)


def _bbox_close(a, b, tolerance: float = 1e-6) -> bool:
    try:
        pair = list(zip((float(v) for v in a), (float(v) for v in b)))
    except (TypeError, ValueError):
        return False
    return len(pair) == 4 and all(abs(x - y) <= tolerance for x, y in pair)


def _occurrence_key(occurrence) -> tuple:
    """`(page, 量化 bbox, index)`——occurrence 的完整身分。

    只比頁碼集合會讓「同頁但框錯了」「index 錯了」「同頁重複次數不同」全部通過，
    之後 manifest / crop / chunk 就會指向不同的實體位置。
    """
    if not isinstance(occurrence, dict):
        return ("?", (), "?")
    try:
        page = int(occurrence.get("page"))
    except (TypeError, ValueError):
        page = -1
    try:
        index = int(occurrence.get("index", 0))
    except (TypeError, ValueError):
        index = -1
    try:
        bbox = tuple(round(float(v), 3) for v in (occurrence.get("bbox") or ()))
    except (TypeError, ValueError):
        bbox = ()
    return (page, bbox, index)


def _occurrence_signature(occurrences) -> tuple:
    """整串 occurrence 的身分（保序、保 multiplicity）。"""
    return tuple(_occurrence_key(item) for item in (occurrences or []))


def _verify_results_match_candidates(fx, filename, plan, results) -> Dict[str, object]:
    """candidate ↔ FigureResult 必須一一對應，否則整份 PDF 零寫入。

    extractor 少回一張、多回一張、回錯 document / 頁 / 框 / occurrence，都代表
    「每張候選可監督」已經破了。這種情況**不得**降級成 `no_pos_cannot_replace`
    （那是保留原文的正常路徑），必須 hard fail。

    `extraction_status` 只有三種合法形狀，其餘一律 hard fail：
    `complete` 必須帶 payload；`failed`（品質失敗、那一張不進 KB）必須是
    `payload=None` **且** `model_input_variant="failed"`；`skipped`（分類器判定不是
    圖面）必須是 `payload=None`，且 `model_input_variant` 指得出分類器看過的那一份
    影像（或 duplicate 哨兵）——半套的結果
    （有 payload 卻宣稱 failed/skipped、或宣稱送過某個 variant）會讓 manifest 記下
    一張誰也說不清有沒有進 KB 的圖。

    occurrence 比對用 `(page, 量化 bbox, index)` 的**保序序列**，不是頁碼集合：
    降成集合的話，錯 bbox、錯 index、同頁重複次數不同都會通過。
    """
    by_fid: Dict[str, object] = {}
    for candidate in plan.candidates:
        if candidate.figure_id in by_fid:
            raise fx.FigureExtractionError(
                f"{filename}: planner 產出兩個 figure_id={candidate.figure_id!r} 的候選。"
                "身分撞號時 manifest / crop / chunk 會指向不同實體位置，整份零寫入。")
        by_fid[candidate.figure_id] = candidate

    seen = set()
    for figure in results:
        if figure.figure_id in seen:
            raise fx.FigureExtractionError(
                f"{filename}: extractor 對 figure_id={figure.figure_id!r} 回了兩次結果")
        seen.add(figure.figure_id)

    missing = sorted(set(by_fid) - seen)
    extra = sorted(seen - set(by_fid))
    if missing or extra:
        raise fx.FigureExtractionError(
            f"{filename}: candidate 與抽取結果不一一對應"
            f"（漏回 {missing}；多回 {extra}）。整份文件零寫入。")

    for figure in results:
        candidate = by_fid[figure.figure_id]
        where = f"{filename} 第 {figure.page} 頁 figure={figure.figure_id}"
        if figure.document_id != plan.document_id:
            raise fx.FigureExtractionError(
                f"{where}: document_id={figure.document_id!r} 與 plan 的 "
                f"{plan.document_id!r} 不同——身分錯配的結果不得入庫")
        if int(figure.page) != int(candidate.page):
            raise fx.FigureExtractionError(
                f"{where}: page 與候選的 {candidate.page} 不同")
        if not _bbox_close(figure.bbox, candidate.bbox):
            raise fx.FigureExtractionError(
                f"{where}: bbox {tuple(figure.bbox)} 與候選的 {tuple(candidate.bbox)} 不同")
        result_occ = _occurrence_signature(figure.occurrences)
        candidate_occ = _occurrence_signature(getattr(candidate, "occurrences", None))
        if result_occ != candidate_occ:
            raise fx.FigureExtractionError(
                f"{where}: occurrence 身分與候選不同（結果 {list(result_occ)}；"
                f"候選 {list(candidate_occ)}）——manifest、crop 與 chunk 會指向不同位置")
        if figure.extraction_status == fx.EXTRACTION_FAILED:
            if figure.payload is not None or figure.model_input_variant != "failed":
                raise fx.FigureExtractionError(
                    f"{where}: extraction_status=failed 卻帶 "
                    f"payload={'有' if figure.payload else '無'}、"
                    f"model_input_variant={figure.model_input_variant!r}"
                    "（應為 None / 'failed'）。整份文件零寫入。")
        elif figure.extraction_status == fx.EXTRACTION_SKIPPED:
            # 「不是圖面」：分類完就停手，所以沒有 payload。但**分類器確實看過那張
            # 圖**，`model_input_variant` 要指得出是哪一份（或 duplicate 哨兵）——
            # 宣稱「無模型輸入」會讓 manifest 說成零 VL，誤判的圖也就無從事後檢查。
            if figure.payload is not None or not str(figure.model_input_variant or ""):
                raise fx.FigureExtractionError(
                    f"{where}: extraction_status=skipped 卻帶 "
                    f"payload={'有' if figure.payload else '無'}、"
                    f"model_input_variant={figure.model_input_variant!r}"
                    "（應為 None / 非空 variant）。整份文件零寫入。")
        elif figure.extraction_status != fx.EXTRACTION_COMPLETE or figure.payload is None:
            raise fx.FigureExtractionError(
                f"{where}: extraction_status={figure.extraction_status!r}、"
                f"payload={'有' if figure.payload else '無'}。整份文件零寫入。")
    return by_fid


def _check_claimed_variants(fx, filename, results, rendered) -> None:
    """`FigureResult.variants` 宣稱送過模型的 variant，必須真的被 renderer 產出過。

    artifact 是人工覆核的唯一依據；讓沒送出的 variant 冒充 evidence，覆核的人
    會對著錯的圖確認。

    native lane **零 VL、沒有任何模型影像輸入**，所以 `variants` 必須是空的
    （契約 §6.4／§15.6，T5 的 writer 也要求「宣告集合 == 實際落盤的模型輸入」）。
    以前這裡特別放行 `variants=["native"]`，等於在 RAG 這一側自造一個 writer 不接受
    的合法形狀——跨模組矛盾因此永遠不會浮出來（契約 §18.4）。
    """
    produced = {}
    for variant in rendered:
        produced.setdefault(getattr(variant, "figure_id", ""), set()).add(
            getattr(variant, "variant_id", ""))
    for figure in results:
        # 重複影像只送一次 VL（契約 §2.5），第二個 occurrence 沿用第一張的 variant id，
        # 所以 `duplicate_of` 指到的那張也算數。
        duplicate_of = (figure.evidence or {}).get("duplicate_of")
        allowed = set(produced.get(figure.figure_id, ()))
        if duplicate_of:
            allowed |= produced.get(duplicate_of, set())
        if figure.model_input_variant == "native" and figure.variants:
            raise fx.FigureExtractionError(
                f"{filename} 第 {figure.page} 頁 figure={figure.figure_id}: "
                f"native lane 沒有模型影像輸入，variants 必須是空的，收到 "
                f"{list(figure.variants)}")
        for variant_id in (figure.variants or []):
            if variant_id not in allowed:
                raise fx.FigureExtractionError(
                    f"{filename} 第 {figure.page} 頁 figure={figure.figure_id}: "
                    f"宣稱送過 variant={variant_id!r}，但 renderer 從未產出它")


def _is_full_image(fx, variant, *, candidate_bbox, where: str) -> bool:
    """這個 `Variant` 是不是**這個候選的完整原圖**？判定只有一份，在門面（契約 §21.1）。

    以前這裡自己抄一份檢查（而且只看 tile flags），`figure_verify` 與 `figure_review`
    各自也有一份——於是每輪終審只有被點名的那一兩端收緊，第三端維持寬鬆，繞道永遠
    在（這條接縫因此被打回四輪）。現在整份判定都由 `figure_extract.is_full_image()`
    出：§6.3 全欄位的 `validate_variant`（含 `digest == sha256(png)`，禁止任何
    coercion）＋ `tile_total == 1` ＋ **`bbox` 必須等於候選框**。

    `candidate_bbox` 是**必填、沒有預設值**：拿不到候選框就沒有比對基準，猜一個等於
    讓「只裁到上緣的局部 crop」冒充完整原圖（終審第六輪 BLOCKER #1 的實測樣態）。

    回傳 `False` 代表「合法、但不是完整原圖」（tile，或只涵蓋局部）；`Variant` 本身
    不合格則 raise `FigureExtractionError`，整份文件零寫入。
    """
    return fx.is_full_image(variant, candidate_bbox=candidate_bbox, where=where)


def _no_full_review_image(judged, candidate, already_sent, where: str) -> str:
    """產不出完整覆核原圖時的診斷訊息：說清楚 renderer 到底給了什麼。

    `judged` 是 `[(variant, 是不是完整原圖), ...]`——判定本身已經由門面做完
    （契約 §21.1），這裡只負責把「為什麼不算」講清楚。四種成因的處置相同（零寫入），
    但訊息不能混：使用者要知道是「什麼都沒給」「只給得出切片」「給了一張宣稱未切片、
    卻只涵蓋候選框一部分的局部 crop」還是「只給得出已經送過模型的那一張」。
    """
    box = tuple(getattr(candidate, "bbox", ()) or ())
    if not judged:
        return (f"{where}: renderer 沒有回任何覆核用影像（候選框 bbox={box}）。"
                "沒有完整原圖就沒有監督依據，整份文件零寫入。")
    local = [item for item, is_full in judged
             if not is_full and getattr(item, "tile_total", None) == 1]
    if local:
        detail = "；".join(
            f"{getattr(item, 'variant_id', '?')!r} bbox="
            f"{tuple(getattr(item, 'bbox', ()) or ())}" for item in local)
        return (f"{where}: renderer 給的「未切片」覆核影像只涵蓋候選框的一部分"
                f"（候選框 bbox={box}，實際 {detail}）——局部 crop 不得冒充完整原圖，"
                "覆核的人會以為自己看到的就是整張圖。整份文件零寫入。")
    if any(is_full for _item, is_full in judged):
        return (f"{where}: renderer 只給得出已經送過模型的 variant "
                f"{sorted(already_sent)}，產不出另一張可放進 review_assets/ 的完整原圖"
                "（同一個 variant_id 不得同時出現在 variants/ 與 review_assets/）。"
                "整份文件零寫入。")
    return (f"{where}: renderer 只給得出切片，產不出完整（未切片）的覆核用原圖。"
            "tile 是模型輸入的切片，不能當成候選原圖，整份文件零寫入。")


def _full_candidate_variants(fx, pdf_doc, candidate) -> List:
    """把候選**整框**render 成一張未切片的圖（只給人覆核，不送模型）。

    做法是沿用 T3 的 renderer，只把 tile plan 換成「一塊涵蓋整個候選框」的單一 tile：
    rotation matrix、zoom、DPI、raster 分流與失敗訊息因此與實際模型輸入走**同一條**
    程式碼路徑。在這裡另寫一套取像的話，旋轉頁上覆核圖會裁到別的地方——拿錯的圖去
    「覆核」比沒有圖更糟。

    候選沒有 tile plan 時原樣呼叫 renderer，由它自己決定（T3 對真的取不了像的候選會
    fail-loud）。這不是 fail-open：不論走哪一條，呼叫端都只接受 `tile_total <= 1` 的產出。
    """
    signals = dict(getattr(candidate, "signals", None) or {})
    plan = dict(signals.get("tile_plan") or {})
    target = candidate
    if plan.get("tiles"):
        full = dict(plan["tiles"][0])
        full.update({"bbox": tuple(candidate.bbox), "tile_index": 0,
                     "tile_total": 1, "overlap_px": 0})
        plan["tiles"] = [full]
        signals["tile_plan"] = plan
        target = _dc_replace(candidate, signals=signals)
    return list(fx.render_candidate_variants(pdf_doc, target) or [])


def _ensure_review_assets(fx, filename, pdf_doc, results, by_fid, rendered) -> Dict[str, List]:
    """每張進 KB 的 figure 都要留得下一張**完整、未切片**的原圖（Go/No-Go 5）。

    回傳 `{figure_id: [Variant, ...]}`，**與實際送模型的 `rendered` 完全分開**
    （契約 §15.6）。以前兩者併在同一個 list，artifact writer 就把「只為覆核而
    render、從未送進模型」的圖寫成實際模型輸入——`review.md` 因此對「模型到底看
    過什麼」說謊。

    §19.2 再加一條：**tile 不算原圖**。候選被切片時，實際模型輸入是一堆
    `crop@Ndpi#tileKofM`，覆核的人看到的就只有切片、拼不回那張圖的完整原貌。以前
    這裡「只要 renderer 回了東西就算數」，於是全是 tile 的候選照樣發布成功。現在：

    - 模型輸入本身就是未切片的一張 → 那就是原圖，已經保存在 `variants/`，不再重複產。
    - 否則（全是 tile，或 native lane 零 VL 完全沒有模型輸入）→ 另外 render 一張
      **未切片的整框 crop** 當 review-only asset。
    - 產不出來 → **在成功 manifest 與任何 KB mutation 之前** fail-loud。

    同一個 `variant_id` 不得同時出現在 `variants/` 與 `review_assets/`（writer 會拒），
    所以已經送過模型的 id 一律排除。

    §21.1 再加一條：「完整原圖」的判定**只有門面那一份**（`figure_extract.is_full_image`），
    而且要比對 `bbox`——tile flags 合法（`tile_index=0, tile_total=1`）但只涵蓋候選框
    一部分的局部 crop 同樣不算原圖。只驗 flags 的話，把第一片 tile 的 bytes 配上合法
    flags 就能發布成功 manifest，而覆核的人以為自己看到的是整張圖。
    """
    sent_ids: Dict[str, set] = {}
    has_full_model_input = set()
    for variant in rendered:
        figure_id = getattr(variant, "figure_id", "")
        sent_ids.setdefault(figure_id, set()).add(getattr(variant, "variant_id", ""))
        candidate = by_fid.get(figure_id)
        if candidate is None:
            # 沒有候選框就沒有「完整原圖」的比對基準。這裡**不猜**（例如拿 variant
            # 自己的 bbox 當基準）——那等於讓局部 crop 自己認證自己。
            raise fx.FigureExtractionError(
                f"{filename}: renderer 產出的 variant figure_id={figure_id!r} 不在候選"
                f"清單裡（候選 {sorted(by_fid)}）。身分對不上就判不了它是不是完整原圖，"
                "整份文件零寫入。")
        if _is_full_image(fx, variant, candidate_bbox=candidate.bbox,
                          where=f"{filename} figure={figure_id} 的模型輸入"):
            has_full_model_input.add(figure_id)

    review_assets: Dict[str, List] = {}
    for figure in results:
        if figure.figure_id in has_full_model_input:
            continue  # 真的送過模型的那一張就是完整原圖，最好的覆核依據
        where = f"{filename} 第 {figure.page} 頁 figure={figure.figure_id}"
        candidate = by_fid[figure.figure_id]
        try:
            produced = _full_candidate_variants(fx, pdf_doc, candidate)
        except Exception as exc:
            raise fx.FigureExtractionError(
                f"{where}: 取不到完整的覆核用原圖（{exc}）。沒有完整原圖就沒有"
                "監督依據，整份文件零寫入。") from exc
        already_sent = sent_ids.get(figure.figure_id, set())
        # 比對基準是**候選框**：宣稱未切片但只涵蓋局部的 crop 不算完整原圖。
        judged = [(variant, _is_full_image(fx, variant, candidate_bbox=candidate.bbox,
                                           where=f"{where} 的覆核用影像"))
                  for variant in produced]
        full = [variant for variant, is_full in judged
                if is_full and getattr(variant, "variant_id", "") not in already_sent]
        if not full:
            raise fx.FigureExtractionError(
                _no_full_review_image(judged, candidate, already_sent, where))
        review_assets[figure.figure_id] = full[:1]
    return review_assets


def _best_effort_review_assets(fx, filename, pdf_doc, figures, by_fid, rendered,
                               already: Dict[str, List]) -> Dict[str, List]:
    """不進 KB 的那幾張（失敗 / 判定不是圖面）的完整覆核影像；產不出來就算了。

    與 `_ensure_review_assets` 的差別**只有一個**：這裡不 fail-loud。那條是「進 KB
    的圖一定要有監督依據」的硬閘；這裡的圖根本不進 KB，為了拿不到一張診斷用影像
    就讓整份文件零寫入，等於把 figure-level 的失敗又升級回 document-level——而那正是
    2026-08-28 花一整輪拆掉的東西。
    """
    produced: Dict[str, List] = {}
    sent_ids: Dict[str, set] = {}
    for variant in rendered:
        sent_ids.setdefault(getattr(variant, "figure_id", ""), set()).add(
            getattr(variant, "variant_id", ""))
    for figure in figures:
        figure_id = str(figure.figure_id)
        candidate = by_fid.get(figure_id)
        if candidate is None or figure_id in already:
            continue
        where = f"{filename} 第 {figure.page} 頁 figure={figure_id}"
        try:
            variants = _full_candidate_variants(fx, pdf_doc, candidate)
            already_sent = sent_ids.get(figure_id, set())
            full = [variant for variant in variants
                    if getattr(variant, "variant_id", "") not in already_sent
                    and _is_full_image(fx, variant, candidate_bbox=candidate.bbox,
                                       where=f"{where} 的覆核用影像")]
        except Exception as exc:  # noqa: BLE001 — 診斷用影像，拿不到不影響已完成的工作
            print(f"  [WARN] {where}: 取不到覆核用原圖（{exc}）；"
                  "這一張不進 KB，仍照常繼續", flush=True)
            continue
        if full:
            produced[figure_id] = full[:1]
    return produced


def _with_reason(figure, slug: str, detail: str):
    return _dc_replace(
        figure,
        reasons=list(figure.reasons or []) + [slug],
        reason_details=list(figure.reason_details or []) + [detail],
    )


def _payload_totals(fx, payload: Dict, kind: str) -> Tuple[Optional[int], Optional[int]]:
    """payload 的實際 row/line 總數（`build_figure_chunks` 會 fail-closed 比對）。"""
    if kind == fx.KIND_TABLE:
        rows = payload.get("rows") or []
        return (rows[-1]["row_index"] if rows else 0), None
    if kind in fx.LINE_KINDS:
        lines = payload.get("lines") or []
        return None, (lines[-1]["line_index"] if lines else 0)
    return None, None


def _existing_human_entries(fx, root_path: Path, kb_path,
                            filename: str) -> Tuple[List[Dict], List[str], Dict[str, int]]:
    """既有 KB ＋ artifacts 裡，這份文件已被人工確認過的 figure。

    回傳 `(usable_rows, unusable_details)`：
      - `usable_rows`：`list_figures()` 的列，可以拿去比對沿用。
      - `unusable_details`：**KB 說有人工確認、但證據不可用**的說明（manifest 壞掉 /
        被回收 / payload 讀不回來 / 簽章缺失 / revision 與 KB 對不上）。
      - `baseline`：讀取當下 `{figure_id: revision}` 的人工確認基線，提交前要在
        exclusive lock 內重驗（並行的 `review_figures fix` 不得被我們蓋掉）。

    這兩者必須分開：以前全部被過濾成空 list，於是「有人工確認但證據壞了」看起來
    跟「第一次 ingest」一模一樣——使用者只會看到 `human_verified` 悄悄消失，chunk 的
    reasons 一個字都沒說。

    `may_carry_over_human_verification()` 直接吃這些列（T5 已把 `source_signature`
    攤到 row 上），不需要再繞去讀 manifest。
    """
    path = Path(kb_path) if kb_path else None
    if path is None or not path.is_file():
        return [], [], {}
    try:
        # 只看 chunk 的 metadata（verification_status / revision），不需要向量：
        # 為了盤點人工確認而去重算整個 KB 的 embedding 是白花錢，而且會讓「舊
        # cache 壞掉」變成「人工修正靜默不沿用」。
        kb = load_knowledge_base(path, _quiet=True, _restore_vectors=False)
    except Exception as exc:  # noqa: BLE001 — 讀不到既有 KB 不該讓 ingest 死掉
        print(f"  [INFO] 讀不到既有知識庫（{exc}），人工確認一律不沿用（fail-closed）",
              flush=True)
        return [], [], {}
    chunks = [c for c in (kb.get("chunks") or [])
              if isinstance(c, dict) and c.get("structured") and c.get("source") == filename]
    baseline = human_revision_baseline(chunks, filename)
    human_ids = set(baseline)
    if not human_ids:
        return [], [], {}
    try:
        rows = fx.list_figures(root_path, chunks)
    except Exception as exc:  # noqa: BLE001
        print(f"  [INFO] 既有覆核清單讀不到（{exc}），人工確認一律不沿用（fail-closed）",
              flush=True)
        return [], [f"既有覆核清單讀不到（{exc}）"], baseline

    usable: List[Dict] = []
    unusable: List[str] = []
    seen_ids = set()
    for row in rows:
        if row.get("verification_status") != fx.VERIF_HUMAN:
            continue
        figure_id = row.get("figure_id")
        seen_ids.add(figure_id)
        why = None
        if not row.get("in_kb"):
            why = "只存在於 artifacts，沒有進 KB"
        elif row.get("payload") is None or row.get("payload_error"):
            why = f"canonical payload 讀不回來（{row.get('payload_error') or 'payload 是 null'}）"
        elif not isinstance(row.get("source_signature"), dict):
            why = "artifact 沒有 source_signature，證明不了來源像素未變"
        else:
            record = row.get("human_verification")
            if not isinstance(record, dict) or record.get("confirmed_against_image") is not True:
                why = "artifact 沒有 confirmed_against_image 的人工確認紀錄"
            elif record.get("revision") != row.get("revision"):
                # KB 是 revision 的唯一真相；紀錄自報的版本與它不同就是 artifact 落後。
                why = (f"人工確認紀錄停在 revision {record.get('revision')!r}，"
                       f"KB 是 {row.get('revision')!r}（artifact 落後）")
        if why is None:
            usable.append(row)
        else:
            unusable.append(f"figure={figure_id} {why}")
    for figure_id in sorted(human_ids - seen_ids):
        unusable.append(f"figure={figure_id} 在 review 清單裡找不到對應的 artifact")
    return usable, unusable, baseline


def _carry_over_human_verification(fx, filename, root_path: Path, kb_path,
                                   by_fid: Dict, results: List) -> Tuple[List, Dict, Dict]:
    """re-ingest 時把既有的人工修正接回來（契約 §15.7）。

    以前這條完全沒接：`may_carry_over_human_verification()` 只有門面與單元測試在用，
    所以 re-ingest 會把 `human_verified` 的 chunk 整批刪掉、換成 revision 1 的機器
    結果——像素與 bbox 一個位元都沒變也一樣。revision 因此**倒退**，違反 §5
    evidence ⑥ 與 revision 單調性。

    成立條件由 `may_carry_over_human_verification()` 判（`asset_digest` + 頁碼 +
    正規化 bbox 全等、舊 entry 是 `human_verified` 且 `confirmed_against_image`）。
    成立 → 沿用人工 canonical payload、`human_verified` 與**舊 revision**；
    不成立 → 一律不沿用，revision 從 1 起。

    回傳 `(results, human_verifications)`。第二個值一定要餵給
    `write_run_artifacts(human_verifications=...)`：**新 manifest 沒有這筆紀錄的話，
    下一輪 re-ingest 的 gate 就會失敗、revision 退回 1**——人工修正只是晚一輪被丟掉。
    寫端刻意不從 `verification_status` 自行合成這筆紀錄（那等於捏造「使用者看過原圖」
    這件事的證據），所以只能由這裡原樣帶過去。

    `human_verification_not_carried` 只在「這份文件真的有既有人工確認」時才記——
    第一次入庫沒有東西可沿用，那不是「沒沿用」，把它記進每個 chunk 的 reasons 只是
    在 REF 上製造雜訊。證據壞掉時原因會一起寫進 `reason_details`，不會靜默降級。
    """
    old_rows, unusable, baseline = _existing_human_entries(
        fx, root_path, kb_path, filename)
    if not old_rows and not unusable:
        return results, {}, baseline
    if unusable:
        for detail in unusable:
            print(f"  [WARN] {filename}: 既有的人工確認無法沿用——{detail}", flush=True)

    carried: List = []
    human_verifications: Dict[str, Dict] = {}
    for figure in results:
        candidate = by_fid[figure.figure_id]
        match = None
        for row in old_rows:
            if row.get("kind") != figure.kind:
                continue
            if fx.may_carry_over_human_verification(row, candidate):
                match = row
                break
        if match is None:
            detail = (f"{filename} figure={figure.figure_id}: 這份文件有既有的人工確認，"
                      "但證明不了這張圖的來源像素與框未變，依 fail-closed 不沿用")
            if unusable:
                detail += "；" + "；".join(unusable)
            carried.append(_with_reason(figure, "human_verification_not_carried", detail))
            continue
        payload = copy.deepcopy(match["payload"])
        row_total, line_total = _payload_totals(fx, payload, figure.kind)
        revision = max(1, int(match.get("revision") or 1))
        print(f"  [INFO] 第 {figure.page} 頁 figure {figure.figure_id}: "
              f"沿用第 {revision} 版人工確認（來源像素與框未變）", flush=True)
        human_verifications[figure.figure_id] = copy.deepcopy(match["human_verification"])
        carried.append(_dc_replace(
            figure,
            payload=payload,
            verification_status=fx.VERIF_HUMAN,
            revision=revision,
            row_total=row_total,
            line_total=line_total,
            reasons=list(figure.reasons or []) + ["human_verification_carried_over"],
            reason_details=list(figure.reason_details or []) + [
                f"{filename} figure={figure.figure_id}: 沿用第 {revision} 版人工確認"
                "（asset_digest / 頁碼 / 正規化 bbox 全等）"],
        ))
    return carried, human_verifications, baseline


def _bbox_slot(page, bbox) -> tuple:
    """(頁碼, 量化 bbox)——用來判斷「這個 occurrence 有沒有自己的候選」。"""
    try:
        return (int(page), tuple(round(float(v), 3) for v in bbox))
    except (TypeError, ValueError):
        return (int(page) if isinstance(page, int) else -1, ())


def _plan_page_replacements(fx, filename, plan, results, by_fid, page_texts):
    """決定哪些「已經在 page markdown 裡的正文」可以安全地被 structured chunk 取代。

    涵蓋兩種來源（見 `_native_span`）：原生 markdown 表格，以及 `pos` 支撐的
    raw markdown 文字（terminal 的 native 正文）。漏掉後者就會讓原始 log 與
    structured chunk 同時留在 KB。

    回傳 `(eligible, dropped, page_items)`。

    **座標單位是「候選自己的 (page, bbox)」**：T3 的候選是 physical 的——同一份內容
    出現在第 1、2 頁就是**兩個**候選，各自有自己的 `pos`，只共享 VL 計算。拿共享的
    occurrences 去逐頁替換會把同一段文字換兩次（而且兩個候選的替換區間會互相重疊，
    整組被判失格）。

    但 occurrence 仍要檢查，而且是**整組 all-or-none**：共享同一串 occurrence 的候選
    要嘛全部可以取代、要嘛全部退回原文。只證明「counterpart 候選存在」不夠——它稍後
    也可能因為 pos 無效而失格，那樣 KB 就會同時留下 raw 與 structured 兩種表示。
    """
    threshold = _iou_threshold()
    candidate_slots = {_bbox_slot(getattr(c, "page", 0), getattr(c, "bbox", ()))
                       for c in plan.candidates}
    eligible, dropped = [], []
    page_items: Dict[int, List] = {}
    pending: Dict[str, tuple] = {}
    problems: Dict[str, str] = {}
    groups: Dict[tuple, List[str]] = {}

    for figure in results:
        candidate = by_fid[figure.figure_id]
        native = _native_span(fx, candidate)
        if native is None:
            # 內容不在 page markdown 裡（raster / 向量圖），沒有東西要取代
            eligible.append(figure)
            continue

        group_key = _occurrence_signature(figure.occurrences) or (figure.figure_id,)
        groups.setdefault(group_key, []).append(figure.figure_id)

        page = int(figure.page)
        raw = page_texts.get(page)
        evidence = (plan.page_evidence or {}).get(page)
        problem = None
        pos = None
        if raw is None:
            problem = f"第 {page} 頁不在 pages 內"
        elif evidence is None:
            problem = f"plan 缺第 {page} 頁 evidence"
        elif getattr(evidence, "raw_markdown", None) != raw:
            problem = f"第 {page} 頁 plan 與 pages 的 raw text 不一致，offset 不可信"
        else:
            missing = [int(o["page"]) for o in (figure.occurrences or [])
                       if _bbox_slot(o["page"], o["bbox"]) not in candidate_slots]
            if missing:
                problem = (f"第 {sorted(set(missing))} 頁的同一份內容沒有自己的候選，"
                           "無法一起取代")
            else:
                box_pos = _native_box_pos(evidence, figure.bbox, threshold, native["classes"])
                declared = _normalize_pos(native.get("pos"))
                found = _normalize_pos(box_pos)
                if declared is not None and found is not None and declared != found:
                    problem = (f"第 {page} 頁 page_boxes 的 pos {found} 與候選宣稱的 "
                               f"{declared} 不一致")
                else:
                    if box_pos is None:
                        box_pos = native.get("pos")
                    problem = _table_pos_problem(fx, raw, box_pos, native.get("markdown"))
                    if problem:
                        problem = f"第 {page} 頁 {problem}"
                    else:
                        pos = _normalize_pos(box_pos)
        if problem:
            problems[figure.figure_id] = problem
        pending[figure.figure_id] = (figure, page, pos, native["kind"], group_key)

    # 共享 occurrence 的整組 all-or-none：一個位置失格，同組其餘位置也不得取代
    for group_key, members in groups.items():
        bad = [fid for fid in members if fid in problems]
        if not bad:
            continue
        for fid in members:
            problems.setdefault(
                fid, f"同一份內容的另一個位置無法安全取代（{problems[bad[0]]}）")

    def _retain(figure, problem: str, native_kind: str) -> None:
        what = "原表格文字" if native_kind == "native_table" else "原始文字"
        print(f"  [WARN] {filename} figure {figure.figure_id} {problem}，"
              f"保留{what}（不重複入庫）", flush=True)
        dropped.append(_with_reason(
            figure, "no_pos_cannot_replace", f"{filename} figure={figure.figure_id}: {problem}"))

    for figure_id, (figure, page, pos, native_kind, _group) in list(pending.items()):
        if figure_id in problems:
            _retain(figure, problems[figure_id], native_kind)
            pending.pop(figure_id)

    for figure, page, pos, _native_kind, _group in pending.values():
        total = figure.row_total if figure.row_total is not None else (figure.line_total or 0)
        text = PDF_TABLE_REPLACED_MARKER.format(
            figure_id=figure.figure_id, page=page, rows=total)
        page_items.setdefault(page, []).append((pos[0], pos[1], figure.figure_id, text))

    # 同頁重疊 → 整個重疊叢集全部失格（刪掉區間 A 會毀掉區間 B 的原文）
    conflicted = set()
    for items in page_items.values():
        for group in _overlap_groups(items):
            if len(group) > 1:
                conflicted |= {entry[2] for entry in group}
    if conflicted:
        # 重疊叢集的成員若與別人共享 occurrence，同組也要一起退回
        for group_key, members in groups.items():
            if any(fid in conflicted for fid in members):
                conflicted |= set(members) & set(pending)
        for figure_id in sorted(conflicted):
            entry = pending.pop(figure_id, None)
            if entry is None:
                continue
            _retain(entry[0], "pos 與同頁另一個區塊重疊", entry[3])
        page_items = {
            page: [entry for entry in items if entry[2] not in conflicted]
            for page, items in page_items.items()
        }
        page_items = {page: items for page, items in page_items.items() if items}

    for figure, _page, _pos, _native_kind, _group in pending.values():
        eligible.append(figure)
    return eligible, dropped, page_items


def _assert_source_identity(fx, filename: str, source_path, root_path, expected: str, *,
                            stage: str) -> None:
    """來源檔還是同一份嗎？不是就 fail-loud（`document_id_for` 的整檔 sha256）。

    整條鏈上有三個檢查點，全部用同一套身分定義：`to_markdown()` 之後（planner 建出
    `document_id` 時）、**發布成功 artifact 之前**、以及 exclusive commit lock 內。
    少任何一個，來源在那段空窗被換掉就會讓 KB 混進兩個版本（契約 §17.3 / §18.2）。
    """
    try:
        current = fx.document_id_for(source_path, root_path)
    except Exception as exc:
        raise fx.FigureExtractionError(
            f"{filename}: {stage}無法重驗來源檔身分（{exc}）。整份文件零寫入。") from exc
    if current != expected:
        raise fx.FigureExtractionError(
            f"{filename}: {stage}發現來源檔已變更（document_id {expected} → {current}）。"
            "整份文件零寫入。")


def _source_identity_snapshot(root_path: Path, file_path: str) -> Optional[str]:
    """`to_markdown()` **之前**的來源身分（`document_id_for` 的內容 digest）。

    刻意**不用** `(size, mtime_ns, inode)` 這種 stat tuple：同尺寸原地改寫在粗時間戳
    檔案系統上、或 mtime 被還原時，那個 tuple 可以完全不變——文字 chunk 來自舊版 A、
    planner 之後以新版 B 建 `document_id`，後面的 hash guard 只會確認 B，KB 就混進
    兩個版本的內容（契約 §17.3）。

    直接用 `document_id_for()`（整檔 sha256）：與 planner、與提交前那道檢查是**同一套
    身分定義**，不另外造第二套比對規則。

    只有「root 內的實體檔案」才有 document identity；不是的話回 `None`（那種情況
    structured lane 本來就不會啟動）。**是**的話一律 fail-loud：算不到 digest 就代表
    我們無法證明後續每一步讀的是同一份檔案，把它吞成 `None` 等於把整條來源身分鏈
    fail-open（契約 §18.2）。
    """
    source_path = Path(file_path)
    if not source_path.is_file() or not _path_within(source_path, root_path):
        return None
    fx = _figure_extract()
    try:
        return fx.document_id_for(source_path, root_path)
    except Exception as exc:
        raise fx.FigureExtractionError(
            f"{source_path.name}: 取不到來源檔的身分 digest（{exc}）。"
            "無法證明後續每一步讀到的是同一份檔案，整份文件零寫入。") from exc


def _failure_reason_slug(reasons) -> str:
    """`reasons` 裡 `extraction_failed` 以外的第一個 slug；沒有就 `unknown`。

    `extraction_failed` 只說「壞了」，不說壞在哪；使用者要看的是後面那個。
    """
    for reason in reasons or []:
        if reason != "extraction_failed":
            return str(reason)
    return "unknown"


def _failure_slug(figure) -> str:
    """失敗 figure 的失敗種類（`reasons` 裡 `extraction_failed` 以外的那一個）。"""
    return _failure_reason_slug(figure.reasons)


def _format_failed_figures(failed: List) -> str:
    """「哪幾張缺席」的單行摘要；沒有失敗就回空字串（不印）。"""
    if not failed:
        return ""
    items = "；".join(
        f"p{int(figure.page)} {figure.kind} {_failure_slug(figure)}"
        for figure in failed
    )
    return f"[figure] 失敗 {len(failed)} 張（不進 KB）：{items}"


def _run_structured_figure_lane(file_path: str, filename: str, pages: List[Dict], *,
                                root: Optional[str], preflight_only: bool,
                                kb_path=None, source_identity: Optional[str] = None) -> Dict:
    """契約 §6.7 的步驟 2–7（合格性判定），一次做完。

    步驟順序不得改：plan → preflight → （routing）→ capability probe → 抽取 →
    review artifacts → page partition 合格性判定。**任何 VL 呼叫與 KB mutation
    之前**必須先過 preflight。
    """
    inactive = {"active": False, "preflight_only": False, "figures": [],
                "failed_figures": [], "failed_summary": "",
                "replacements": {}, "page_source": {},
                "evidence_ref": {}, "guard": None, "absent": []}
    root_path = _figure_root(root)
    source_path = Path(file_path)
    if source_identity is not None:
        # **不論 structured lane 有沒有啟動**都要帶 guard：lane 沒啟動的 PDF 一樣會
        # 產生文字 chunk，來源在中途被換掉時，同一份 KB 就會混進 A 版文字與 B 版
        # figure（契約 §18.2）。`human_baseline=None` 表示這條
        # 路徑沒有讀過人工確認基線，提交前只驗來源身分。
        inactive = {**inactive, "guard": {
            "root": str(root_path),
            "document_id": source_identity,
            "source_path": str(source_path),
            "source": filename,
            "human_baseline": None,
            "wrote_run": False,
            # 這條路徑沒有建立 run，也沒有任何 figure 抽壞；提交時的摘要仍然要
            # 讀得到這兩個 key（少一個就得在提交點寫 `.get()` 分支，那就是第二份
            # 對 guard 形狀的假設）。
            "run_id": "",
            "failed": [],
        }}
    if kb_path is None:
        # 呼叫端沒指定就用專案預設的知識庫（`mcp_server.ingest_document` 也是這一份）。
        kb_path = root_path / getattr(config_module, "KNOWLEDGE_FILE", "knowledge.json")

    def _blocked(message: str, slug: str) -> Dict:
        print(f"  [INFO] {message}", flush=True)
        if preflight_only:
            raise PdfPreflightUnavailable(
                f"{filename}: 無法產生 figure preflight 報告——{message}")
        # lane 沒啟動＝這份 PDF 的圖一張都不會進 KB。只在**上游真的宣稱有圖**時列帳：
        # 純文字 PDF 每次都印一句「圖沒進去」就是罐頭提示，使用者會學會跳過整段。
        return {**inactive, "absent": [
            {"page": page, "bbox": None, "channel": "page",
             "reason": f"structured_lane_inactive:{slug}"}
            for page in _pdf_picture_pages(pages)
        ]}

    if not source_path.is_file():
        return _blocked("沒有實體檔案（stub / 測試路徑），PDF 結構化 figure lane 不啟動",
                        "no_source_file")
    if not _path_within(source_path, root_path):
        return _blocked(
            f"{filename} 不在專案根 {root_path} 內，無法建立 document identity"
            "（契約 §2.5），PDF 結構化 figure lane 不啟動；該檔的圖不會進知識庫",
            "outside_root")

    fx = _figure_extract()
    try:
        pdf_doc = _open_pdf_document(file_path)
    except Exception as exc:
        raise fx.FigureExtractionError(
            f"無法開啟 PDF 進行 figure 抽取: {filename}: {exc}") from exc

    try:
        plan = fx.plan_document_figures(
            file_path, pages, root=str(root_path), pdf_doc=pdf_doc)

        # to_markdown() 讀到的那一份，與 planner 建 document_id 的那一份，必須是同一個檔。
        # 比的是內容 digest（`document_id_for` 的整檔 sha256），不是 stat tuple：
        # 同尺寸原地改寫騙得過 stat，騙不過 hash（契約 §17.3）。
        if source_identity is not None and plan.document_id != source_identity:
            raise fx.FigureExtractionError(
                f"{filename}: 解析文字與偵測候選之間來源檔被換掉了"
                f"（document_id {source_identity} → {plan.document_id}）。"
                "文字 chunk 與 figure 會來自兩個版本，整份文件零寫入。")

        # lane signal 的型別/存在性在**印出成功的 preflight 報告之前**就要驗完：
        # 報告是「預算是這樣、可以開跑」的宣告，拿沒驗過的 signal 算出來的預算不算數。
        for candidate in plan.candidates:
            fx.read_native_lane(candidate)

        # ---- 預算：在任何 VL 呼叫、embedding、KB mutation 之前 ----
        try:
            fx.check_preflight(plan)
        except fx.FigureBudgetError as exc:
            hint = ("  改用 CLI 先看報告再分批處理：\n"
                    f"    python3 RAG.py {file_path} <knowledge.json> --preflight")
            print(f"[ERROR] {filename} figure preflight 超出上限："
                  "未呼叫任何 VL、未算 embedding、未寫入 knowledge.json。", flush=True)
            print(fx.format_preflight_report(plan), flush=True)
            print(hint, flush=True)
            raise fx.FigureBudgetError(f"{exc}\n{hint}") from exc

        if preflight_only:
            print(fx.format_preflight_report(plan), flush=True)
            return {**inactive, "active": True, "preflight_only": True}

        candidates, filtered_absent = _structured_candidates(fx, plan)
        if not candidates:
            # 零候選也可能是「這頁的圖全部落在 lane 之外」，缺席帳照樣要交出去。
            return {**inactive, "absent": _absent_from_plan(plan, filtered_absent)}
        if not plan.document_id:
            # T3 對「身分建立不了」的降級 plan 會回零候選；真走到這裡代表契約破了，
            # 而空 document_id 會一路帶到 chunk 與 artifact 目錄上。
            raise fx.FigureExtractionError(
                f"{filename}: plan 有候選卻沒有 document_id，身分無法建立。整份文件零寫入。")
        plan = _dc_replace(plan, candidates=candidates)

        # ---- capability probe：需要 VL 時才做，且在 KB mutation 前 ----
        # lane 判定的唯一 reader 在門面（契約 §17.4）：planner / verifier / 這裡讀同一份
        # 實作，`"false"` 這種非 bool 值不會在一邊是錯誤、在另一邊被 truthiness 當成 native。
        vl_candidates = [c for c in candidates if not fx.read_native_lane(c)]
        needs_vl = bool(vl_candidates) and int(
            (plan.preflight or {}).get("vl_calls_max", 0) or 0) > 0
        if needs_vl:
            kinds = set()
            for candidate in vl_candidates:
                # native lane 永遠不呼叫 VL（契約 §12.1），所以只有這些候選要 probe
                if candidate.kind == fx.KIND_UNKNOWN:
                    kinds |= {fx.KIND_TABLE, fx.KIND_TERMINAL}
                elif candidate.kind == fx.KIND_RASTER:
                    kinds |= set(fx.FIGURE_KINDS)
                elif candidate.kind in (fx.KIND_TABLE, fx.KIND_TERMINAL):
                    kinds.add(candidate.kind)
            fx.ensure_capability(
                base_url=LLAMA_VL_BASE_URL, model=VL_MODEL, kinds=kinds)

        run_id = fx.new_run_id()
        document_id = plan.document_id
        # `FigureResult` 的凍結欄位沒有 asset_digest / page_rect，所以「同一張圖的人工
        # 確認能不能沿用」必須由 T7 從 Candidate 把簽章帶進 manifest（沒帶＝永不沿用）。
        source_signatures = {
            candidate.figure_id: fx.source_signature(candidate)
            for candidate in candidates
        }
        rendered: List = []
        seen_variants = set()

        def _record(doc_arg, candidate):
            produced = fx.render_candidate_variants(doc_arg, candidate) or []
            for variant in produced:
                key = (getattr(variant, "figure_id", ""), getattr(variant, "variant_id", ""))
                if key in seen_variants:
                    continue
                seen_variants.add(key)
                rendered.append(variant)
            return produced

        def _record_generated(variant):
            """登記 verifier 由原始 pixels 產生的實際模型輸入（例如 ruled-grid 正規化）。"""
            key = (getattr(variant, "figure_id", ""), getattr(variant, "variant_id", ""))
            if key not in seen_variants:
                seen_variants.add(key)
                rendered.append(variant)
            return variant

        def _progress(*parts):
            print("  " + " ".join(str(part) for part in parts), flush=True)

        def _write_failed(partial) -> None:
            """失敗也要留 per-figure 的覆核紀錄——best effort，不得遮蔽原始例外。"""
            try:
                fx.write_run_artifacts(
                    root_path, document_id=document_id, run_id=run_id,
                    figures=partial, variants=rendered, failed=True,
                    preflight=plan.preflight, stats=plan.stats,
                    source_signatures=source_signatures, review_assets={},
                    human_verifications=None)
            except Exception as art_exc:  # noqa: BLE001
                print(f"  [WARN] 失敗的 review artifact 寫不出來"
                      f"（原始錯誤仍會拋出）: {art_exc}", flush=True)

        # run_id 建立之後的**每一步**都在同一個錯誤交易裡：抽取本身、一一對應
        # 驗證、variant 宣稱驗證、覆核影像——任何一步失敗都要留下 per-figure 的
        # failed artifact，否則「失敗的候選仍可監督」這條就只在 extractor 自己
        # 拋錯時成立。
        results = []
        failed_figures: List = []
        skipped_figures: List = []
        try:
            extracted = list(fx.extract_document_figures(
                plan, pdf_doc=pdf_doc, page_evidence=plan.page_evidence,
                vl_base_url=LLAMA_VL_BASE_URL, vl_model=VL_MODEL,
                render_variants=_record, record_generated_variant=_record_generated,
                on_progress=_progress))
            # 品質失敗的那幾張（`extraction_status=failed`）**不進 KB**，但要留在同一份
            # `failed:false` 的 manifest 裡供覆核。從這裡開始只有 complete 的往下走：
            # chunk、page partition、覆核影像、人工確認沿用都只對得起有 payload 的結果。
            # 一一對應驗證是唯一的例外——它要看的是「每張候選都有交代」，所以吃未拆的
            # 完整 list。
            failed_figures = [figure for figure in extracted
                              if figure.extraction_status == fx.EXTRACTION_FAILED]
            # 「不是圖面」與「抽壞了」要分開：前者沒有下一步（封面 / logo / 照片），
            # 混進 failed 會讓每份 PDF 的通知都掛著幾筆假警報，真的抽壞的那幾張
            # 就淹在裡面。兩者都不進 KB，但只有 failed 進待覆核與通知。
            skipped_figures = [figure for figure in extracted
                               if figure.extraction_status == fx.EXTRACTION_SKIPPED]
            results = [figure for figure in extracted
                       if figure.extraction_status == fx.EXTRACTION_COMPLETE]

            # renderer 可能先產生原圖，verifier 再以同尺寸的衍生 variant 取代它做
            # structured extraction。只有 FigureResult.variants 宣告的 id 才真的送過
            # 模型；其餘 renderer 中間產物不准混進 variants/ 冒充模型輸入。
            # skipped 也算數：分類器真的把那張圖送進了模型，影像必須留得下來，
            # 否則被誤判成「不是圖面」的圖就沒有任何東西可以事後檢查。
            declared_inputs = {
                (figure.figure_id, variant_id)
                for figure in results + skipped_figures
                for variant_id in (figure.variants or [])
            }
            # 抽壞的那幾張靠 **send ledger**（`evidence["sent_variants"]`）：
            # `_failed_result` 的 `variants=[]`（中止的結果沒辦法可靠宣告自己送過
            # 什麼），照 declared 濾會把導致失敗的那張圖一起刪掉——事後補 render 的
            # 完整原圖重現不了它。但也**不能整包留**：三片裡第一片就失敗時，後兩片
            # 根本沒送出去，寫進 `variant_paths`（「送進模型的影像」）就是謊報；
            # table 的 grid 正規化同理，extractor 只看過 `+grid` 那一份。
            sent_inputs = {
                (figure.figure_id, str(variant_id))
                for figure in failed_figures + skipped_figures
                for variant_id in ((figure.evidence or {}).get("sent_variants") or [])
            }
            keep = declared_inputs | sent_inputs
            rendered[:] = [
                variant for variant in rendered
                if (getattr(variant, "figure_id", ""),
                    getattr(variant, "variant_id", "")) in keep
            ]

            by_fid = _verify_results_match_candidates(fx, filename, plan, extracted)
            _check_claimed_variants(fx, filename, results + skipped_figures, rendered)
            # 契約 §15.7：extract_document_figures 之後、build_figure_chunks 之前，
            # 而且要在寫 manifest 之前（新 run 的 manifest 也要記到人工 payload）。
            results, human_verifications, human_baseline = _carry_over_human_verification(
                fx, filename, root_path, kb_path, by_fid, results)
            review_assets = _ensure_review_assets(
                fx, filename, pdf_doc, results, by_fid, rendered)
            # 抽壞 / 判定不是圖面的那幾張也留一張完整原圖：沒有影像的失敗 artifact
            # 診斷不了、也覆核不了。**best-effort**——這幾張本來就不進 KB，為了它們
            # 把整份文件變成零寫入是把 figure-level 的失敗又升級回 document-level。
            review_assets.update(_best_effort_review_assets(
                fx, filename, pdf_doc, failed_figures + skipped_figures, by_fid,
                rendered, review_assets))
        except fx.FigureError as exc:
            # `.failed` 是單一 FigureResult（T4 的 `_failed_result`），`.results` 是 list；
            # 兩種形狀都要吃下去——這裡再拋 TypeError 會把原始抽取錯誤整個蓋掉。
            partial = list(getattr(exc, "results", None) or [])
            failed = getattr(exc, "failed", None)
            if isinstance(failed, (list, tuple)):
                partial.extend(failed)
            elif failed is not None:
                partial.append(failed)
            if not partial:
                # post-validation 失敗時 extractor 沒有掛 partial，用它回的那批
                partial = list(results) + list(failed_figures) + list(skipped_figures)
            _write_failed(partial)
            raise

        page_texts = _first_page_texts(pages)
        eligible, dropped, page_items = _plan_page_replacements(
            fx, filename, plan, results, by_fid, page_texts)

        # figure_index 的最終編號要在**寫 artifact 之前**完成：manifest 記 T4 的內部
        # 序號、KB/REF 記另一套序號，覆核的人與檢索就會用到兩套
        # 身分（同一張圖在 manifest 是 1、在 REF 是 2）。
        sequence: Dict[int, int] = {}
        numbered_eligible = []
        for figure in sorted(eligible, key=lambda f: (f.page, f.figure_index)):
            page = int(figure.page)
            sequence[page] = sequence.get(page, 0) + 1
            numbered_eligible.append(_dc_replace(figure, figure_index=sequence[page]))
        numbered_dropped = []
        for figure in sorted(dropped, key=lambda f: (f.page, f.figure_index)):
            page = int(figure.page)
            sequence[page] = sequence.get(page, 0) + 1
            numbered_dropped.append(_dc_replace(figure, figure_index=sequence[page]))
        # 抽壞的那幾張排在最後編號：它們不進 KB，不該把已入庫那幾張的頁內序號往後推。
        numbered_failed = []
        for figure in sorted(failed_figures, key=lambda f: (f.page, f.figure_index)):
            page = int(figure.page)
            sequence[page] = sequence.get(page, 0) + 1
            numbered_failed.append(_dc_replace(figure, figure_index=sequence[page]))
        # 判定不是圖面的排在最後：它們不進 KB、不進待覆核，但要留在 manifest 裡，
        # 「這張圖去哪了」才查得到。
        numbered_skipped = []
        for figure in sorted(skipped_figures, key=lambda f: (f.page, f.figure_index)):
            page = int(figure.page)
            sequence[page] = sequence.get(page, 0) + 1
            numbered_skipped.append(_dc_replace(figure, figure_index=sequence[page]))
        failed_summary = _format_failed_figures(numbered_failed)
        if failed_summary:
            _progress(failed_summary)
        # 發布成功 manifest **之前**先重驗來源身分：先寫再驗的話，身分不符時會留下
        # 一份 `failed:false` 的 manifest，宣稱一次根本沒有成立的成功 run（契約 §18.2）。
        _assert_source_identity(fx, filename, source_path, root_path, document_id,
                                stage="發布 review artifact 之前")

        manifest = fx.write_run_artifacts(
            root_path, document_id=document_id, run_id=run_id,
            figures=(numbered_eligible + numbered_dropped + numbered_failed
                     + numbered_skipped),
            variants=rendered, failed=False,
            preflight=plan.preflight, stats=plan.stats,
            source_signatures=source_signatures, review_assets=review_assets,
            human_verifications=human_verifications)
        evidence_ref = fx.evidence_ref_for(document_id, run_id)
        expected = (root_path / evidence_ref).resolve()
        if manifest is None or Path(manifest).resolve() != expected:
            raise fx.FigureExtractionError(
                f"{filename}: write_run_artifacts 回傳的 {manifest!r} 不是預期的 "
                f"{expected}——chunk 的 evidence_ref 會指到別的檔")
        if not expected.is_file():
            raise fx.FigureExtractionError(
                f"{filename}: manifest {expected} 不存在或不是一般檔案，"
                "寫進 KB 的 evidence_ref 會是懸空的 locator")

        replacements = {}
        for page, items in page_items.items():
            raw = page_texts[page]
            replacements[page] = [
                (start_pos, end_pos, _marker_piece(raw, start_pos, end_pos, text))
                for start_pos, end_pos, _figure_id, text in items
            ]

        return {
            "active": True,
            "preflight_only": False,
            "figures": numbered_eligible,
            # 不進 KB，但呼叫端要印得出「哪幾張缺席」——ingest 仍然 exit 0，
            # 使用者只從 chunk 數看不出少了什麼。
            "failed_figures": numbered_failed,
            "failed_summary": failed_summary,
            "replacements": replacements,
            # 套替換前要確認「當初算 pos 的那份文字」與現在手上這份是同一個字串，
            # 畸形 metadata 讓兩個 page dict 撞同一頁碼時才不會切錯位置
            "page_source": {page: page_texts[page] for page in replacements},
            # 有東西、卻沒有經過這條 lane 進 KB 的頁與區域（頁碼 / bbox / 固定 slug）。
            "absent": _absent_from_plan(plan, filtered_absent + [
                {"page": int(figure.page),
                 "bbox": [float(v) for v in figure.bbox],
                 "channel": "candidate", "reason": "raster_not_a_figure"}
                for figure in numbered_skipped
            ]),
            "evidence_ref": {figure.figure_id: evidence_ref for figure in numbered_eligible},
            "guard": {
                "root": str(root_path),
                "document_id": document_id,
                "source_path": str(source_path),
                "source": filename,
                "human_baseline": human_baseline,
                "wrote_run": True,
                "run_id": str(run_id),
                # 抽壞的那幾張不進 KB，所以提交點掃 KB chunks 是看不到它們的；
                # 這裡順手記下來（零內容，只有頁碼／身分／固定 slug），提交後的
                # 摘要才講得出「這一次少了哪幾張」。
                "failed": [
                    {"page": int(figure.page), "kind": str(figure.kind),
                     "figure_id": str(figure.figure_id),
                     "figure_index": int(figure.figure_index),
                     "reason": _failure_slug(figure)}
                    for figure in numbered_failed
                ],
            },
        }
    finally:
        with contextlib.suppress(Exception):
            pdf_doc.close()


def extract_pdf_document(file_path: str, *, preflight_only: bool = False,
                         root: Optional[str] = None) -> ExtractedDocument:
    """提取 PDF 內容，保留頁碼、文件類型、章節。

    逐頁切 chunk（維持既有切點），但同時把每頁正規化後的文字串成整份
    raw_text 並記下頁 span——章節範圍因此是文件級的，不再受制於「這一頁看得
    到哪些標題」。

    圖面只有**一條** lane（2026-08-30 起；契約 §0.1 / §13.1）：有結構性原生證據的
    候選，以及夠大的純 raster / picture 候選，一律走 **structured lane**——raster 先
    依圖片分類成 table / terminal / prose / diagram（或「不是圖面」而跳過），再產生
    canonical JSON payload 與 `origin="figure_*"`。schema / validator 失敗一律
    `FigureExtractionError`，**不降級成自由文字**（workflow §7）。

    structured lane 沒收的頁與區域就是**缺席**：不入庫、不做自由文字描述，改由
    `format_absent_regions()` 與 ingest 摘要逐筆列出頁碼、bbox 與原因。以前那條
    legacy 自由文字 lane（`origin="diagram"`）已經移除——它沒有 ▯、沒有 review
    artifact、沒有 evidence_ref，事後從 KB 與摘要都看不出「這段是看圖說故事」。

    `FigureBudgetError` / `FigureCapabilityError` / `FigureExtractionError` /
    `FigureValidationError` 全部在 `_commit_document_to_kb` 之前拋出，所以是「整份
    文件零寫入」；這與「PDF 本身打不開 → 回空 document」的既有語意刻意不同：
    前者是圖會無聲消失的部分成功，後者是整份明顯失敗。

    `preflight_only=True` 只跑到預算計算並印報告，**零寫入、零 VL、零 embedding**；
    算不出報告（PDF 解析失敗、結構化 lane 未啟動）時 raise `PdfPreflightUnavailable`
    ——沒有報告卻回成功是假成功（契約 §11.4）。
    """
    return _extract_pdf_document_impl(
        file_path, preflight_only=preflight_only, root=root, kb_path=None)


def _extract_pdf_document_impl(file_path: str, *, preflight_only: bool,
                               root: Optional[str],
                               kb_path) -> ExtractedDocument:
    """`extract_pdf_document` 的實作。

    公開介面是 Gate 0 §6.7 凍結的三個參數；`kb_path` 是 re-ingest 的 human
    verification carry-over（§15.7）需要的**內部**資料通道，只給 `add_document` /
    `process_file_document` 這條路徑用，不擴張公開 signature。
    """
    # 延遲載入 pymupdf4llm（只有 PDF 模式需要）
    pymupdf4llm = check_pymupdf4llm()

    filename = Path(file_path).name

    # 解析文字之前先釘住來源身分：`to_markdown()` 與後面 planner 建 document_id
    # 之間換檔的話，文字 chunk 與 structured figure 會來自兩個版本（TOCTOU）。
    source_identity = _source_identity_snapshot(_figure_root(root), file_path)

    try:
        pages = pymupdf4llm.to_markdown(file_path, page_chunks=True, write_images=False)
    except Exception as e:
        if preflight_only:
            # regular ingest 維持「警告後回空 document」；preflight 不行——
            # 沒有報告卻 exit 0 會讓使用者以為預算沒問題
            raise PdfPreflightUnavailable(
                f"{filename}: 無法解析 PDF，產不出 figure preflight 報告: {e}") from e
        print(f"  [WARN] 無法處理 PDF: {e}")
        return ExtractedDocument(raw_text="", source=filename)

    doc_type = detect_doc_type(filename)

    # 改進：若檔名無法識別類型，使用內容特徵輔助判斷
    if doc_type == 'doc' and pages:
        # 取第一頁內容作為特徵樣本
        first_page_content = pages[0].get('text', '') if pages else ''
        doc_type = detect_doc_type_by_content(first_page_content, doc_type)

    # 根據文件類型取得 chunk 設定
    chunk_size, chunk_overlap = get_chunk_settings(doc_type)

    lane = _run_structured_figure_lane(
        file_path, filename, pages,
        root=root, preflight_only=preflight_only, kb_path=kb_path,
        source_identity=source_identity,
    )
    if preflight_only:
        return ExtractedDocument(raw_text="", source=filename, doc_type=doc_type)

    chunks: List[Dict] = []
    page_texts: List[str] = []
    page_spans: List[Tuple[int, int, int]] = []
    # 頁碼 → 該頁文字 chunk 數；figure chunk 的 chunk_index 接在同頁文字 chunk
    # 之後，chunk id（source::pN::cM::hash）才不會與文字 chunk 共用索引空間
    text_chunk_counts: Dict[int, int] = {}
    offset = 0  # 下一頁在 raw_text 中的起點
    pending_replacements = dict(lane["replacements"])
    # 這次 ingest **有內容、但沒有進 KB** 的頁與區域。提交點的摘要靠它講「少了什麼」。
    absent: List[Dict] = list(lane["absent"])
    # 旋轉頁正文退路的兩個懶開 handle（`None` 未開、`False` 開不起來）：
    #   probe_doc  — 路徑開檔，只讀 `page.rotation`（便宜；空白頁很常見）
    #   stream_doc — bytes 開檔，只在**真的有旋轉頁**時才建（見 `_open_pdf_from_bytes`）
    probe_doc: List = [None]
    stream_doc: List = [None]

    def _lazy_open(holder: List, opener, what: str):
        if holder[0] is None:
            try:
                holder[0] = opener(file_path)
            except Exception as exc:  # noqa: BLE001 — 退路失敗只降級成「列帳」
                holder[0] = False
                print(f"  [WARN] {filename}: 開不了 PDF 做{what}: {exc}", flush=True)
        return holder[0]

    def _recover_page_text(page_number: int) -> str:
        """markdown 交白卷的頁的正文退路；抽不出來就記進 absent 帳（不 raise）。"""
        probe = _lazy_open(probe_doc, _open_pdf_document, "旋轉頁偵測")
        if not probe:
            return ""
        try:
            rotation = _pdf_page_rotation(probe, page_number)
        except Exception as exc:  # noqa: BLE001
            print(f"  [WARN] {filename}: 第 {page_number} 頁讀不到 rotation: {exc}",
                  flush=True)
            return ""
        if not rotation:
            # rotation == 0 卻沒有 markdown 的頁就是真的沒有文字層（掃描頁 / 空白頁）。
            # 再抽一次只會得到同一個空字串，而「有圖沒文字」由 figure 那條帳處理。
            return ""
        recovered = ""
        stream = _lazy_open(stream_doc, _open_pdf_from_bytes, "旋轉頁正文退路")
        if stream:
            try:
                recovered = _pdf_unrotated_page_markdown(stream, page_number)
            except Exception as exc:  # noqa: BLE001
                print(f"  [WARN] {filename}: 第 {page_number} 頁正文退路失敗: {exc}",
                      flush=True)
        if not recovered.strip():
            absent.append({"page": page_number, "bbox": None,
                           "reason": f"rotated_{rotation}_text_unavailable",
                           "channel": "text"})
            return ""
        print(f"  [INFO] {filename}: 第 {page_number} 頁旋轉 {rotation}°，"
              "上游 markdown 為空；已把 /Rotate 歸零後重抽正文", flush=True)
        return recovered

    for page_info in pages:
        page_num = _pdf_page_number(page_info.get('metadata'))
        raw_page_text = page_info.get('text', '') or ''

        # page partition：用 pos 在 **raw page text**（normalize 前）精確切掉已由
        # structured chunk 收錄的表格，換成單行 marker。同頁碼的第二個 page dict
        # 不再套第二次（那份文字與 plan 看到的不是同一份）。
        pieces = pending_replacements.pop(page_num, None)
        if pieces:
            if lane["page_source"].get(page_num) != raw_page_text:
                # 靜默跳過會留下「原 markdown 表 + structured chunk」兩份互相競爭的
                # 版本（workflow §8-4）。寧可整份零寫入。
                raise _figure_extract().FigureExtractionError(
                    f"{filename} 第 {page_num} 頁：套用 page partition 時的文字與"
                    "算 pos 時的那份不同，offset 不可信。整份文件零寫入。")
            raw_page_text = _apply_page_replacements(raw_page_text, pieces)

        content = raw_page_text.strip()

        if not content:
            # `pieces` 非空代表這一頁本來就有原文（pos 算得出來），不可能走到這裡；
            # 真走到就是 partition 把整頁吃掉了，退路會拿回**未經 partition** 的
            # 文字，讓被 structured chunk 收錄的表再出現一次。寧可缺席。
            content = "" if pieces else _recover_page_text(page_num).strip()
        if not content:
            continue

        # 先正規化再切：raw_text 必須就是 splitter 看到的那份文字，offset 才對得上
        page_text = normalize_document_text(content)
        if not page_text:
            continue

        # 使用帶章節的切分（根據文件類型調整 chunk 大小）
        chunk_results = split_by_semantic_with_sections(
            page_text,
            max_chars=chunk_size,
            overlap_chars=chunk_overlap,
            pre_normalized=True,
        )
        for i, chunk_data in enumerate(chunk_results):
            # 根據內容判斷是否為警告類型
            chunk_type = detect_content_type(chunk_data["content"], doc_type)

            # section 先放 splitter 的 page-local 值，最後由 apply_section_titles()
            # 統一改成文件級真相（舊碼在這裡用 last_section 補，會補到過期章節）
            chunks.append({
                "source": filename,
                "page": page_num,
                "chunk_index": i,
                "content": chunk_data["content"],
                "type": chunk_type,
                "section": chunk_data["section"],
                "heading_hierarchy": chunk_data.get("heading_hierarchy", ""),
                **_retrieval_prefix_metadata(chunk_data),
                **_chunk_locator_metadata(chunk_data, offset),
            })
        # 累加而非覆寫：畸形 metadata 讓兩個 page dict 撞同一頁碼時，
        # figure chunk 的起始 index 也不能與前一批文字 chunk 相撞
        text_chunk_counts[page_num] = (
            text_chunk_counts.get(page_num, 0) + len(chunk_results)
        )

        page_spans.append((page_num, offset, offset + len(page_text)))
        page_texts.append(page_text)
        offset += len(page_text) + len(PAGE_SEPARATOR)

    for holder in (probe_doc, stream_doc):
        # 退路 handle 用完就關：stream_doc 在記憶體裡改過 /Rotate，留著只會讓誰誤用。
        if holder[0]:
            with contextlib.suppress(Exception):
                holder[0].close()
        holder[0] = False

    if pending_replacements:
        # 有 structured chunk 要取代的表，但那一頁根本沒被走訪到 → 原表會留在別處
        raise _figure_extract().FigureExtractionError(
            f"{filename}: 第 {sorted(pending_replacements)} 頁的表格已改由 structured "
            "chunk 收錄，但那些頁沒有出現在 pages 走訪中，原 markdown 會與 structured "
            "chunk 兩份並存。整份文件零寫入。")

    raw_text = PAGE_SEPARATOR.join(page_texts)
    document = ExtractedDocument(
        raw_text=raw_text,
        sections=extract_sections(raw_text, page_spans),
        chunks=chunks,
        source=filename,
        doc_type=doc_type,
        page_spans=page_spans,
    )
    # 章節對齊只作用在文字 chunk：figure chunk 的 section 來自 VL 描述自己的
    # markdown 標題（與獨立圖片入庫一致），拿 PDF 文件級章節去蓋會蓋錯座標系。
    document.assign_section_indices()
    document.apply_section_titles()
    setattr(document, _FIGURE_PRUNE_ATTR, lane["guard"])
    setattr(document, _ABSENT_ATTR, absent)

    if lane["figures"]:
        before = len(document.chunks)
        # figure_index 已經在寫 artifact 之前編好（manifest / KB / REF 共用同一批）
        numbered = lane["figures"]
        document.chunks.extend(build_structured_figure_document(
            numbered,
            source=filename,
            doc_type=doc_type,
            next_chunk_index=text_chunk_counts,
            evidence_ref_by_figure=lane["evidence_ref"],
            # caption / 章節在**文字 chunk 都建好之後**才算得出來：它們的座標系是
            # 文件級 raw_text 的 offset，而那份 raw_text 就是上面那個迴圈拼出來的。
            context_by_figure=_figure_retrieval_context(document, numbered),
        ))
        print(f"[INFO] 結構化 figure 入庫: {len(numbered)} 張 → "
              f"{len(document.chunks) - before} 個 structured chunk", flush=True)
    if lane.get("failed_summary"):
        # 品質失敗只讓那一張缺席，ingest 仍然 exit 0——所以「少了哪幾張」必須自己
        # 說出來，否則使用者只會看到一個比預期少的 chunk 數。
        print(lane["failed_summary"], flush=True)
    absent_summary = format_absent_regions(filename, absent)
    if absent_summary:
        print(absent_summary, flush=True)
    return document


def extract_pdf(file_path: str) -> List[Dict]:
    """提取 PDF 內容（只要 chunks 的相容入口）"""
    return extract_pdf_document(file_path).chunks


# ============================================================
# PDF 逐頁抽取的輔助
# ============================================================
def _pdf_page_number(meta: Optional[Dict]) -> int:
    """pymupdf4llm 頁碼相容 helper（唯一定義）。

    >= 1.x 的 key 是 page_number（1-based）；舊版是 page（0-based）。舊 key
    硬讀會讓所有 chunk 都變第 1 頁。
    """
    meta = meta or {}
    page_num = meta.get('page_number')
    if page_num is None:
        page_num = meta.get('page', 0) + 1
    return int(page_num)


def _pdf_page_rotation(pdf_doc, page_number: int) -> int:
    """第 page_number 頁的 /Rotate；超出範圍回 0。"""
    index = page_number - 1
    if index < 0 or index >= int(pdf_doc.page_count):
        return 0
    return int(getattr(pdf_doc[index], "rotation", 0) or 0)


def _open_pdf_from_bytes(file_path: str):
    """**從 bytes** 開同一份 PDF（旋轉頁正文退路專用）。

    ⚠️ 釘版 pymupdf 1.28.0 / pymupdf4llm 1.28.0 實測的上游地雷：同一個**路徑**上跑過
    一次 `to_markdown()` 之後，再 `pymupdf.open(<同一路徑>)`、把 /Rotate 歸零、對那一頁
    重抽，`text` **恆為空字串**；改用 `pymupdf.open(stream=...)` 就正常。同一支程式裡
    先抽別的檔不會有事——是路徑本身被記住了。（`page.get_text()` 在兩種情況下都拿得到
    字，所以症狀只出現在 pymupdf4llm 這一層，不容易從 pymupdf 那邊看出來。）

    退路是「正文救不救得回來」的最後一關，不能建立在這個行為上。用 bytes 開檔多讀一次
    檔案，而整份 sha256（`_source_identity_snapshot`）本來每次 ingest 都會讀一次，
    這不是新的成本量級。**只在真的有旋轉頁需要救時才呼叫。**
    """
    import pymupdf

    return pymupdf.open(stream=Path(file_path).read_bytes(), filetype="pdf")


def _pdf_unrotated_page_markdown(stream_doc, page_number: int) -> str:
    """把 `stream_doc` 第 page_number 頁的 /Rotate 歸零後重抽 markdown。

    釘版 pymupdf4llm 1.28.0 對 `rotation != 0` 的頁，`to_markdown(page_chunks=True)`
    回的 `text` **恆為空字串**（上游限制，`figure_candidates` 檔頭已記載；同一頁
    `page.get_text()` 有全文）。舊碼在 `if not content: continue` 直接跳過那一頁，
    於是橫放的 register map / 大表頁整頁正文零 chunk、零 WARN、摘要零提示。

    退路刻意**仍走 pymupdf4llm**，只是先把該頁的 /Rotate 歸零：`page.get_text()` 拿得到
    字，但那是另一種形狀的文字（沒有標題、沒有 code fence），混進來會讓同一份 PDF 有
    兩套切點。歸零後產出的 markdown 與其他頁逐字同一種形狀。

    **不寫檔、不共用別人的 handle**：structured lane 的 `pdf_doc` 拿 rotation 做座標
    換算（`page.derotation_matrix`），在它身上歸零等於把整條幾何算錯。
    """
    index = page_number - 1
    if index < 0 or index >= int(stream_doc.page_count):
        return ""
    stream_doc[index].set_rotation(0)
    produced = check_pymupdf4llm().to_markdown(
        stream_doc, pages=[index], page_chunks=True, write_images=False)
    for info in produced or []:
        if not isinstance(info, dict):
            continue
        if _pdf_page_number(info.get("metadata")) != page_number:
            # 上游若改了 `pages=` 的頁碼語義，硬用會把 A 頁的正文寫成 B 頁的 chunk。
            continue
        return str(info.get("text") or "")
    return ""


def _open_pdf_document(file_path: str):
    """render 用的 pymupdf 開檔（獨立函式：測試以假 renderer 取代）。"""
    import pymupdf  # pymupdf4llm 的相依，PDF 模式必然裝了
    return pymupdf.open(file_path)


def extract_text_file_document(file_path: str) -> ExtractedDocument:
    """提取純文字檔案（md, txt），包含文件類型和章節"""
    filename = Path(file_path).name
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        print(f"  [WARN] 無法讀取檔案: {e}")
        return ExtractedDocument(raw_text="", source=filename)

    doc_type = detect_doc_type(filename)

    # 改進：若檔名無法識別類型，使用內容特徵輔助判斷
    if doc_type == 'doc':
        doc_type = detect_doc_type_by_content(content, doc_type)

    # 根據文件類型取得 chunk 設定
    chunk_size, chunk_overlap = get_chunk_settings(doc_type)

    return build_text_document(
        content,
        source=filename,
        base_type=doc_type,
        doc_type=doc_type,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        page=1,  # 純文字檔案視為單頁
    )


def extract_text_file(file_path: str) -> List[Dict]:
    """提取純文字檔案（只要 chunks 的相容入口）"""
    return extract_text_file_document(file_path).chunks


def extract_binary_document(file_path: str) -> ExtractedDocument:
    """提取 binary/ELF 內容（hex dump、magic、可讀字串、ELF symbol 等）。

    用 media.read_binary_for_ingest 抽出可分析的 Markdown 報告：遇到 ELF magic 走
    elf_analysis 的長版多視角報告（summary + 完整 symbol 表 + memmap + relocation 逐筆
    含 caller + DWARF 函式 / 型別 + 全部分類字串，上限 config.BIN_ELF_INGEST_MAX_CHARS），
    不受 analyze_file 單次 25K 的限制。報告裡 【...】 章節標記會被轉成 ## 標題，方便語意切分。
    """
    filename = Path(file_path).name
    # 延遲載入 media（其他模式不需要）
    try:
        from media import read_binary_for_ingest, set_sandbox_root
    except ImportError as e:
        print(f"[ERROR] binary/ELF 模式需要 media 模組: {e}")
        return ExtractedDocument(raw_text="", source=filename)

    p = Path(file_path).resolve()
    if not p.is_file():
        print(f"  [WARN] 檔案不存在: {file_path}")
        return ExtractedDocument(raw_text="", source=filename)

    # read_binary_for_ingest 內建沙箱（_SANDBOX_ROOT），這裡設成檔案所在目錄即可
    set_sandbox_root(str(p.parent), allow_external=False)

    content = read_binary_for_ingest(str(p))
    if (not content
            or content.startswith("[BIN 錯誤]")
            or content.startswith("[ELF 錯誤]")):
        print(f"  [WARN] binary 分析失敗: {content[:200] if content else '空結果'}")
        return ExtractedDocument(raw_text="", source=p.name)

    # 把 【標題】(尾巴) 轉成 ## 標題 尾巴，讓 split_by_semantic_with_sections 抓得到章節
    # （media.py 的報告會出現「【可讀字串（含 offset）】共 3 個」這種尾巴帶文字的行）
    content = re.sub(r'^【(.+?)】(.*)$', r'## \1\2', content, flags=re.MULTILINE)

    return build_text_document(
        content,
        source=p.name,
        base_type="binary",
        doc_type="binary",
        page=1,
        extra={"origin": "binary"},
    )


def extract_binary(file_path: str) -> List[Dict]:
    """提取 binary/ELF 內容（只要 chunks 的相容入口）"""
    return extract_binary_document(file_path).chunks


def process_file_document(file_path: str, *, kb_path: Optional[str] = None) -> ExtractedDocument:
    """根據檔案類型選擇處理方式，回傳文件級單一真相

    `kb_path` 只給 PDF 的 structured figure lane 用：re-ingest 要先讀既有 KB 才知道
    哪些 figure 已經被人工確認過（契約 §15.7）。沒給就用專案預設的知識庫。
    """
    ext = Path(file_path).suffix.lower()

    if ext == ".pdf":
        return _extract_pdf_document_impl(
            file_path, preflight_only=False, root=None, kb_path=kb_path)
    elif ext in {".md", ".txt"}:
        return extract_text_file_document(file_path)
    elif ext in BINARY_EXTENSIONS or ext in ELF_EXTENSIONS:
        return extract_binary_document(file_path)
    else:
        return ExtractedDocument(raw_text="", source=Path(file_path).name)


def process_file(file_path: str) -> List[Dict]:
    """根據檔案類型選擇處理方式（只要 chunks 的相容入口）"""
    return process_file_document(file_path).chunks

# ============================================================
# 聊天截圖處理
# ============================================================
def extract_chat_from_screenshot(image_path: str) -> str:
    """
    使用 VL 模型從截圖中提取聊天內容並整理成結構化摘要
    """
    import base64

    # 讀取圖片並轉 base64
    with open(image_path, 'rb') as f:
        image_data = base64.b64encode(f.read()).decode('utf-8')

    # 取得副檔名
    ext = Path(image_path).suffix.lower()

    # 提示詞：要求 VL 模型提取並整理聊天內容
    # 增加「原始摘錄」層，降低幻覺風險
    prompt = """請分析這張聊天截圖，並整理成結構化的技術知識文件。

**重要**：請盡量忠實呈現原文，不要推測或補完看不清楚的內容。

請按以下格式輸出：

# [主題標題]

## 原始對話摘錄
（請盡量逐字轉錄對話內容，看不清楚的地方標註 [看不清楚] 或 [模糊]）
```
[人物A]: ...
[人物B]: ...
...
```

## 背景/問題
[簡述討論的背景或問題]

## 重點摘要
- [重點1]
- [重點2]
- ...

## 詳細步驟（如果有的話）
1. [步驟1]
2. [步驟2]
...

## 注意事項
- [注意事項1]
- [注意事項2]
...

## 相關檔案/工具
- [檔案或工具名稱]: [說明]

---
請用繁體中文輸出，保留原文中的專有名詞和指令。
如果截圖內容不是聊天對話，請直接描述圖片中的技術資訊。
若有任何不確定的內容，請明確標註「推測」或「不確定」。"""

    try:
        return llama_client.vision_completion(
            base_url=LLAMA_VL_BASE_URL,
            prompt=prompt,
            image_base64=image_data,
            mime_type=IMAGE_MIME_TYPES[ext],
            model=VL_MODEL,
            max_tokens=VL_INGEST_MAX_TOKENS,
            temperature=0.2,
            timeout=VL_INGEST_TIMEOUT,
        )
    except Exception as e:
        print(f"[ERROR] VL 模型處理失敗: {e}")
        return ""


def build_chat_document(image_name: str, content: str) -> ExtractedDocument:
    """VL 抽出的聊天文字 → ExtractedDocument（互動式與自動入庫共用同一份切法）"""
    return build_text_document(
        content,
        source=f"chat_{image_name}",
        base_type='chat',
        doc_type='chat',
        extra={"origin": "screenshot"},  # 標記來源是截圖
    )


def process_chat_screenshot_document(image_path: str) -> ExtractedDocument:
    """處理聊天截圖，提取並整理成知識區塊"""
    print(f"[INFO] 使用 VL 模型分析截圖...")

    # 提取聊天內容
    content = extract_chat_from_screenshot(image_path)

    if not content:
        return ExtractedDocument(raw_text="", source=f"chat_{Path(image_path).name}")

    print(f"[INFO] 提取完成，內容長度: {len(content)} 字元")
    print("-" * 40)
    print(content[:500] + "..." if len(content) > 500 else content)
    print("-" * 40)

    return build_chat_document(Path(image_path).name, content)


def process_chat_screenshot(image_path: str) -> List[Dict]:
    """處理聊天截圖（只要 chunks 的相容入口）"""
    return process_chat_screenshot_document(image_path).chunks


# ============================================================
# 技術圖片處理
# ============================================================
# 提示詞：針對技術圖片的分析（獨立 --image 入庫與 PDF 內嵌圖共用同一份）
# 增加「原始文字摘錄」層，降低幻覺風險
_TECHNICAL_IMAGE_PROMPT = """請詳細分析這張技術圖片，並整理成結構化的技術文件。

**重要**：請盡量忠實呈現圖中文字，不要推測或補完看不清楚的內容。

這可能是以下類型的圖片：
- 系統架構圖 / 方塊圖
- 記憶體映射圖 / 位址空間
- 硬體連接圖 / 介面圖
- 流程圖 / 狀態機
- 資料流程圖
- 時序圖
- 其他技術示意圖

請按以下格式輸出：

# [圖片主題/名稱]

## 原始文字摘錄
（請列出圖中所有可辨識的文字標註，看不清楚的標註 [模糊]）
```
- [文字1]
- [文字2]
- [位址/數值]: [對應文字]
...
```

## 概述
[簡述這張圖的用途和主要內容]

## 主要元件/模組
- [元件1]: [說明]
- [元件2]: [說明]
...

## 連接關係/資料流
- [來源] → [目標]: [說明]
- [來源] ↔ [目標]: [雙向關係說明]
...

## 位址/數值資訊（如果有的話）
| 位址/參數 | 值 | 說明 |
|----------|-----|------|
| ... | ... | ... |

## 重要細節
- [細節1]
- [細節2]
...

## 相關術語
- [術語]: [解釋]
...

---
請用繁體中文輸出，保留原文中的專有名詞、位址、數值。
盡可能完整描述圖中的所有資訊，包括文字標註、箭頭方向、顏色區分等。
若有任何不確定的內容，請明確標註「推測」或「不確定」。"""


def _describe_technical_image_base64(image_base64: str, mime_type: str) -> str:
    """技術圖片 VL 抽述的嚴格核心：連不上/逾時/回空一律 raise。

    PDF 內嵌圖路徑（hard fail，整份文件零寫入）直接用這個核心；
    `extract_info_from_image`（--image 路徑）維持既有的吃例外回空字串行為。
    """
    content = llama_client.vision_completion(
        base_url=LLAMA_VL_BASE_URL,
        prompt=_TECHNICAL_IMAGE_PROMPT,
        image_base64=image_base64,
        mime_type=mime_type,
        model=VL_MODEL,
        max_tokens=VL_INGEST_MAX_TOKENS,
        temperature=0.2,
        timeout=VL_INGEST_TIMEOUT,
    )
    if not content or not content.strip():
        raise RuntimeError(f"VL 回傳空內容（{LLAMA_VL_BASE_URL}）")
    return content


def extract_info_from_image(image_path: str) -> str:
    """
    使用 VL 模型從技術圖片中提取資訊並整理成結構化文件
    適用於：架構圖、流程圖、記憶體映射圖、硬體方塊圖等
    """
    import base64

    # 讀取圖片並轉 base64
    with open(image_path, 'rb') as f:
        image_data = base64.b64encode(f.read()).decode('utf-8')

    try:
        return _describe_technical_image_base64(
            image_data, IMAGE_MIME_TYPES[Path(image_path).suffix.lower()]
        )
    except Exception as e:
        print(f"[ERROR] VL 模型處理失敗: {e}")
        return ""


def build_image_document(image_name: str, content: str) -> ExtractedDocument:
    """VL 抽出的圖片說明 → ExtractedDocument（互動式與自動入庫共用同一份切法）"""
    return build_text_document(
        content,
        source=f"image_{image_name}",
        base_type='diagram',
        doc_type='diagram',
        extra={"origin": "image"},  # 標記來源是技術圖片
    )


def process_technical_image_document(image_path: str) -> ExtractedDocument:
    """處理技術圖片，提取並整理成知識區塊"""
    print(f"[INFO] 使用 VL 模型分析技術圖片...")

    # 提取圖片資訊
    content = extract_info_from_image(image_path)

    if not content:
        return ExtractedDocument(raw_text="", source=f"image_{Path(image_path).name}")

    print(f"[INFO] 提取完成，內容長度: {len(content)} 字元")
    print("-" * 40)
    print(content[:500] + "..." if len(content) > 500 else content)
    print("-" * 40)

    return build_image_document(Path(image_path).name, content)


def process_technical_image(image_path: str) -> List[Dict]:
    """處理技術圖片（只要 chunks 的相容入口）"""
    return process_technical_image_document(image_path).chunks


# ============================================================
# 自動快取（追溯 VL/URL 分析的原始內容）
# ============================================================
RAG_CACHE_DIR = ".rag_cache"


def _ensure_cache_dir() -> Path:
    """確保快取目錄存在"""
    cache_dir = Path(RAG_CACHE_DIR)
    cache_dir.mkdir(exist_ok=True)
    return cache_dir


def _save_to_cache(source_name: str, content: str, source_type: str, metadata: dict = None):
    """
    自動將分析結果存入快取目錄，供日後追溯

    Args:
        source_name: 來源名稱（如 teams_chat.png, https://...）
        content: 分析後的 markdown 內容
        source_type: 類型（chat/image/url）
        metadata: 額外的 metadata（如 title, url 等）
    """
    cache_dir = _ensure_cache_dir()

    # 生成快取檔名
    safe_name = re.sub(r'[^\w\-.]', '_', source_name)[:80]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cache_file = cache_dir / f"{source_type}_{safe_name}_{timestamp}.md"

    # 寫入快取（失敗時僅警告，不中斷流程）
    try:
        with open(cache_file, 'w', encoding='utf-8') as f:
            f.write(f"<!-- 來源: {source_name} -->\n")
            f.write(f"<!-- 類型: {source_type} -->\n")
            f.write(f"<!-- 生成時間: {datetime.now().isoformat()} -->\n")
            if metadata:
                for k, v in metadata.items():
                    f.write(f"<!-- {k}: {v} -->\n")
            f.write("\n")
            f.write(content)
        return cache_file
    except Exception as e:
        print(f"[WARN] 快取寫入失敗: {e}")
        return None


# ============================================================
# Embedding
# ============================================================
def _load_embedding_cache(cache_path: Path) -> Dict[str, List[float]]:
    """載入 embedding 快取

    快取格式：{content_hash: embedding}
    使用內容雜湊作為 key，避免重複計算相同內容的 embedding
    """
    if not cache_path.exists():
        return {}
    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # 驗證 embedding model 一致
        if data.get('model') != EMBEDDING_MODEL:
            print(f"  [INFO] Embedding model 變更，清除快取")
            return {}
        return data.get('cache', {})
    except Exception as e:
        print(f"  [WARN] 載入 embedding 快取失敗: {e}")
        return {}


def _save_embedding_cache(cache_path: Path, cache: Dict[str, List[float]]):
    """儲存 embedding 快取"""
    try:
        data = {
            'model': EMBEDDING_MODEL,
            'updated_at': datetime.now().isoformat(),
            'count': len(cache),
            'cache': cache
        }
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"  [WARN] 儲存 embedding 快取失敗: {e}")


def _content_hash(content: str) -> str:
    """計算內容雜湊（用於快取 key）"""
    import hashlib
    return hashlib.md5(content.encode('utf-8')).hexdigest()


def _embedding_input(chunk: Dict) -> str:
    """content-only 組字（gate 訊號）。組字規則的唯一定義在 context_signals。"""
    return context_signals.gate_embedding_input(chunk)


def _embed_text_cached(text: str, cache: Dict, state: Dict) -> List[float]:
    """算一段文字的 embedding，先查內容雜湊快取。

    快取 key 是「實際送出的字串」的雜湊，所以 retrieval（含 ctx）與 gate
    （純內容）兩種組字自然分屬不同 key，不會互相污染。
    """
    key = _content_hash(text)
    # 舊版可能留下空向量，空值視為 miss 並重新請求。
    cached = cache.get(key)
    if cached:
        state["hits"] += 1
        return cached

    try:
        embedding = llama_client.embed_one(
            base_url=LLAMA_EMBED_BASE_URL,
            content=text,
            model=EMBEDDING_MODEL,
            timeout=120,
        )
    except Exception as exc:
        raise RuntimeError(
            f"embedding server unreachable at {LLAMA_EMBED_BASE_URL}: {exc}. "
            "Check the 8081 llama-server or AICODE_LLAMA_EMBED_BASE_URL."
        ) from exc

    if not embedding:
        raise RuntimeError(
            f"embedding server returned an empty vector at {LLAMA_EMBED_BASE_URL}. "
            "Check the 8081 llama-server or AICODE_LLAMA_EMBED_BASE_URL."
        )

    cache[key] = embedding
    state["updated"] = True
    return embedding


def _generate_embedding_fields(
    chunks: List[Dict],
    cache_dir: Path = None,
    *,
    retrieval: bool,
    gate: bool,
) -> List[Dict]:
    """替 chunks 產生 retrieval / gate 兩套向量（各自可選）。

    chunk 沒有 ctx 時兩套組字完全相同，直接共用同一個向量，不會多打一次
    embedding server——「沒開 contextual」的部署因此不會多付任何成本。
    """
    total = len(chunks)
    if not total:
        return chunks

    if cache_dir is None:
        cache_dir = Path.cwd()
    cache_path = cache_dir / EMBEDDING_CACHE_FILE
    cache = _load_embedding_cache(cache_path)
    state = {"hits": 0, "updated": False}

    for i, chunk in enumerate(chunks):
        # 進度顯示
        if (i + 1) % 10 == 0 or i == 0 or i == total - 1:
            status = f"(快取命中: {state['hits']})" if state["hits"] > 0 else ""
            print(f"  Embedding: {i + 1}/{total} {status}", end='\r')

        retrieval_text = context_signals.retrieval_embedding_input(chunk, use_ctx=True)
        gate_text = context_signals.gate_embedding_input(chunk)

        if retrieval:
            chunk['embedding'] = _embed_text_cached(retrieval_text, cache, state)
        if gate:
            if gate_text == retrieval_text and chunk.get('embedding'):
                chunk['embedding_gate'] = list(chunk['embedding'])
            else:
                chunk['embedding_gate'] = _embed_text_cached(gate_text, cache, state)

    # 儲存更新後的快取
    if state["updated"]:
        _save_embedding_cache(cache_path, cache)
        print(f"\n  [INFO] Embedding 快取已更新 ({len(cache)} 項)")
    elif state["hits"] > 0:
        print(f"\n  [INFO] 快取命中 {state['hits']}/{total} ({state['hits']*100//total}%)")
    else:
        print()  # 換行

    return chunks


def generate_embeddings(
    chunks: List[Dict], cache_dir: Path = None, *, with_gate: bool = False
) -> List[Dict]:
    """為所有 chunks 生成 embeddings

    - 快取 key = 實際送出字串的 MD5 雜湊
    - with_gate=True 時同時補上 content-only 的 gate 向量（決策訊號）
    """
    return _generate_embedding_fields(
        chunks, cache_dir, retrieval=True, gate=with_gate
    )


def generate_gate_embeddings(chunks: List[Dict], cache_dir: Path = None) -> List[Dict]:
    """只補 content-only 的 gate 向量（retrieval 向量已經有了的情況）。"""
    return _generate_embedding_fields(
        chunks, cache_dir, retrieval=False, gate=True
    )

# ============================================================
# 主程式
# ============================================================
def load_knowledge_base(
    output_path: Path, *, _already_locked: bool = False, _quiet: bool = False,
    _restore_vectors: bool = True,
) -> Dict:
    """載入現有知識庫，不存在則建立空的

    `_already_locked` 給「load→改→save 要在同一把鎖裡完成」的呼叫端用。flock 是
    綁在 open file description 上的：同一個行程另開一個 fd 再上鎖會**擋住自己**，
    所以不能靠重入。它同時也關掉 cache 自動重建：重算要連 embedding server，
    在別人的 exclusive 交易裡做網路 I/O 會把整個 KB 鎖住幾十分鐘。呼叫端一律
    先在鎖外 load 一次（那次會把 cache 補好），鎖內這次就只是重讀。

    JSON 不在了 ＝ 空知識庫，而且**順手清掉無主的 embeddings cache**：使用者用
    檔案總管刪掉 knowledge.json 之後，不該還留著一份看起來像知識庫的向量檔。
    這一步不建立任何目錄，`--preflight` 那條零寫入路徑才不會長出 `.codetrail/`。

    `_restore_vectors=False` 只讀 JSON，不去碰向量。給「拿 chunk metadata 就夠」
    的呼叫端用：fresh ingest（那些 chunk 馬上就要被丟掉）與 figure 的人工確認
    盤點。少了這個旗標，fresh 會因為「舊 cache 壞掉」而在**清空之前**就中止——
    但 fresh 的直覺正好相反：舊向量怎樣都不重要，反正整批要換掉。
    """
    if output_path.exists():
        @contextlib.contextmanager
        def _maybe_lock():
            if _already_locked:
                yield
            else:
                with knowledge_store_lock(output_path, exclusive=False):
                    yield

        with _maybe_lock():
            with open(output_path, 'r', encoding='utf-8') as f:
                kb = json.load(f)

        # store lock 只保護「讀到一致的 JSON」。補向量（可能要連 embedding server
        # 重算幾分鐘）一律在鎖外做：持著 shared lock 打網路的話，期間任何 writer
        # 都拿不到 exclusive lock。cache 自帶身分，驗證不需要鎖；重算的發布會在
        # kb_cache 裡自己取一次短鎖並確認 generation 沒變（見 kb_cache.rebuild）。
        # `_already_locked=True` 的呼叫端本來就在別人的交易裡，那條路徑不重算。
        if _restore_vectors:
            _restore_embeddings_from_npz(
                kb, output_path, allow_rebuild=not _already_locked)
        if not _quiet:
            print(f"[INFO] 載入現有知識庫: {len(kb.get('chunks', []))} 個區塊")
        return kb

    if not _already_locked:
        # JSON 不在了 ＝ 空 KB，順手清掉無主 cache。要在 exclusive lock 內確認
        # 「真的沒有 JSON」，不然會刪掉別人正在提交的那一份（見 purge_orphans）。
        # `_already_locked` 的呼叫端已經在別人的交易裡，不能再去拿鎖（會擋住自己）。
        kb_cache.purge_orphans(output_path, announce=not _quiet)

    # 建立空的知識庫
    return {
        "metadata": {
            "created_at": datetime.now().isoformat(),
            "embedding_model": EMBEDDING_MODEL,
            "chunk_size": CHUNK_SIZE,
            "total_documents": 0,
            "total_chunks": 0,
            "documents": []
        },
        "chunks": []
    }

def _chunks_content_hash(
    chunks: List[Dict], schema: str = LEGACY_CONTENT_HASH_SCHEMA
) -> str:
    """計算 chunks hash；實作在 context_signals，載入端用的是同一份。"""
    return context_signals.chunks_content_hash(chunks, schema=schema)


def _restore_embeddings_from_npz(
    kb: Dict, output_path: Path, *, allow_rebuild: bool = True
) -> bool:
    """把 chunks 的向量補回來（來源是程式自管的 embeddings cache）。

    RAG JSON 為了減少體積不保存 embedding；增量新增文件前必須先把舊 embeddings
    還原，否則下一次 save 會把舊 chunks 寫成零向量。

    驗證與重建的規則**只有一份**，在 ``kb_cache``：查詢端（knowledge.KnowledgeBase）
    走的是同一個函式，兩邊不可能各自漂移出一套寬鬆版本。這裡只決定「驗不過時要
    不要重算」：``allow_rebuild=False``（別人的 exclusive 交易裡）一律 fail-loud。

    KB 有任何 ctx 時，gate 矩陣（content-only 決策訊號）必須同時存在——缺了就
    fail-loud，絕不退回「拿 contextual 向量當 gate 用」：那等於讓生成脈絡抬高
    的分數去通過拒答閘，是分數面的循環 grounding。
    """
    chunks = kb.get("chunks", [])
    if not chunks:
        return False
    saved_metadata = kb.get("metadata", {})
    saved_model = str(saved_metadata.get("embedding_model", ""))
    if saved_model and saved_model != EMBEDDING_MODEL:
        raise KnowledgeStoreError(
            "embedding model mismatch: "
            f"JSON={saved_model}, configured={EMBEDDING_MODEL}. "
            "Rebuild the whole knowledge base; mixing vectors from different "
            "models is forbidden."
        )

    needs_gate = context_signals.has_any_ctx(chunks)
    have_retrieval = all(chunk.get("embedding") for chunk in chunks)
    have_gate = all(chunk.get("embedding_gate") for chunk in chunks)
    if have_retrieval and (have_gate or not needs_gate):
        _, dimension = validate_embeddings(chunks)
        stored_dimension = saved_metadata.get("embedding_dimension")
        if stored_dimension and int(stored_dimension) != dimension:
            raise KnowledgeStoreError(
                "embedding dimension metadata mismatch: "
                f"JSON={stored_dimension}, vectors={dimension}"
            )
        return True

    matrices, stale = kb_cache.locate(output_path, chunks, saved_metadata,
                                     mutate=allow_rebuild)
    if matrices is None:
        if not allow_rebuild:
            raise kb_cache.fatal(stale)
        # 重算會直接把兩套向量掛回 chunks（validate_embeddings 已驗過）
        kb_cache.rebuild(output_path, chunks, saved_metadata, reason=stale)
        return True

    embeddings = matrices.embeddings
    gate_embeddings = matrices.gate_embeddings
    for index, chunk in enumerate(chunks):
        if not chunk.get("embedding"):
            row = embeddings[index]
            chunk["embedding"] = row.tolist() if hasattr(row, "tolist") else list(row)
        if gate_embeddings is not None and not chunk.get("embedding_gate"):
            row = gate_embeddings[index]
            chunk["embedding_gate"] = row.tolist() if hasattr(row, "tolist") else list(row)
    return True


def save_knowledge_base(kb: Dict, output_path: Path, *, _already_locked: bool = False):
    """儲存知識庫

    改進：將 embeddings 完全移到 .npz，JSON 只存文字與 metadata
    - 大幅減少 JSON 檔案大小
    - 加速 JSON 解析
    - .npz 使用壓縮格式，整體儲存更有效率

    KB 裡只要有任何一個 chunk 帶 ctx，就同時寫出 content-only 的 gate 矩陣，
    retrieval schema 也跟著換成 contextual 版本。完全沒有 ctx 的 KB 走的還是
    單一矩陣 + 舊 schema，輸出與加入 contextual retrieval 之前逐位元組相同。
    """
    chunks = kb.get("chunks", [])
    needs_gate = context_signals.has_any_ctx(chunks)

    missing_embeddings = [chunk for chunk in chunks if not chunk.get("embedding")]
    if missing_embeddings:
        print(f"[INFO] {len(missing_embeddings)} 個 chunks 缺少 embedding，明確重算後再提交")
        generate_embeddings(
            missing_embeddings, cache_dir=output_path.parent, with_gate=needs_gate
        )
    if needs_gate:
        missing_gate = [chunk for chunk in chunks if not chunk.get("embedding_gate")]
        if missing_gate:
            print(f"[INFO] {len(missing_gate)} 個 chunks 缺少 gate embedding，明確重算後再提交")
            generate_gate_embeddings(missing_gate, cache_dir=output_path.parent)

    # 更新 metadata
    kb["metadata"]["updated_at"] = datetime.now().isoformat()
    kb["metadata"]["total_documents"] = len(kb["metadata"]["documents"])
    kb["metadata"]["total_chunks"] = len(kb["chunks"])

    retrieval_schema = (
        context_signals.CONTEXTUAL_INPUT_SCHEMA if needs_gate else EMBEDDING_INPUT_SCHEMA
    )
    content_hash = _chunks_content_hash(kb["chunks"], schema=retrieval_schema)
    gate_hash = (
        _chunks_content_hash(kb["chunks"], schema=context_signals.GATE_SCHEMA)
        if needs_gate else None
    )
    # ★ 路徑一定要走驗過的那條，而且**整段提交期間持有那個目錄的 fd**：先檢查
    # 路徑再用是 check-then-use，`.codetrail` 在中途被換成指向 sandbox 外的
    # symlink，NDA 向量就寫到外面去了。空 KB 不建目錄（零寫入）。
    emb_target = (kb_cache.prepare_cache_target(output_path) if kb["chunks"]
                  else kb_cache.checked_cache_file(output_path))
    with kb_cache.cache_dir_fd(output_path, create=bool(kb["chunks"])) as emb_fd:
        _, saved_emb_path = save_knowledge_store_atomic(
            kb,
            output_path,
            embedding_file=emb_target,
            embedding_model=EMBEDDING_MODEL,
            content_hash=content_hash,
            content_hash_schema=retrieval_schema,
            chunk_ids=kb_cache.chunk_row_ids(kb["chunks"]),
            embedding_dir_fd=emb_fd,
            legacy_companion=kb_cache.legacy_companion(output_path),
            gate_content_hash=gate_hash,
            gate_content_hash_schema=context_signals.GATE_SCHEMA if needs_gate else None,
            already_locked=_already_locked,
        )
    if saved_emb_path:
        emb_size = saved_emb_path.stat().st_size / 1024 / 1024
        gate_note = "（含 gate 矩陣）" if needs_gate else ""
        print(f"     Embeddings: {emb_size:.2f} MB{gate_note}（程式自管的 cache，"
              f"使用者不需要備份/複製）")
    else:
        # 檔案已經在上面那次原子提交裡（鎖內）刪掉了。這裡**什麼都不做**：
        # 鎖已經放掉，刪檔會刪到別人剛提交的 cache，連 rmdir 空目錄都會讓正在
        # 提交的 writer 收到 ENOENT（Linux 允許 rmdir 一個已被開啟的空目錄）。
        # 留下的空目錄由下一次 purge_orphans()（鎖內）收乾淨。
        print("     Embeddings: 空知識庫，已清除 embeddings cache")

    file_size = output_path.stat().st_size / 1024 / 1024  # MB
    print(f"\n[OK] 知識庫已更新!")
    print(f"     檔案: {output_path.absolute()}")
    print(f"     大小: {file_size:.2f} MB")
    print(f"     文件數: {kb['metadata']['total_documents']}")
    print(f"     區塊數: {kb['metadata']['total_chunks']}")


def remove_document_from_knowledge_base(output_path: Path, source: str) -> Dict:
    """Transactionally remove one source while preserving aligned remaining vectors."""
    output_path = Path(output_path)
    target = Path(source).name
    if not output_path.is_file():
        raise KnowledgeStoreError(f"knowledge JSON does not exist: {output_path}")

    # 先在鎖外載一次：cache 缺了/過期了就在這裡重算完。重算要連 embedding server，
    # 放進下面的 exclusive 交易會把整個 KB 鎖住整段網路 I/O。
    load_knowledge_base(output_path, _quiet=True)

    with knowledge_store_lock(output_path, exclusive=True):
        try:
            with open(output_path, "r", encoding="utf-8") as handle:
                kb = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise KnowledgeStoreError(f"failed to read {output_path}: {exc}") from exc
        _restore_embeddings_from_npz(kb, output_path, allow_rebuild=False)

        chunks = list(kb.get("chunks", []))
        documents = list(kb.setdefault("metadata", {}).get("documents", []))
        kept_chunks = [
            chunk for chunk in chunks
            if Path(str(chunk.get("source", ""))).name != target
        ]
        kept_documents = [
            document for document in documents
            if Path(str(document)).name != target
        ]
        removed_chunks = len(chunks) - len(kept_chunks)
        removed_documents = len(documents) - len(kept_documents)
        if removed_chunks == 0 and removed_documents == 0:
            return {
                "target": target,
                "removed_chunks": 0,
                "removed_documents": 0,
                "remaining_chunks": len(chunks),
                "remaining_documents": len(documents),
                "sources": sorted({Path(str(c.get("source", ""))).name for c in chunks}),
            }

        kb["chunks"] = kept_chunks
        kb["metadata"]["documents"] = kept_documents
        # 身分紀錄要跟著走，否則它會變成刪不掉的墓碑：使用者照著衝突訊息刪了，
        # 再灌同名的另一份還是被擋。（提交路徑上的 sync 也會收，這裡是明講意圖。）
        forget_document_identity(kb["metadata"], target)
        save_knowledge_base(kb, output_path, _already_locked=True)

        return {
            "target": target,
            "removed_chunks": removed_chunks,
            "removed_documents": removed_documents,
            "remaining_chunks": len(kept_chunks),
            "remaining_documents": len(kept_documents),
            "sources": sorted({
                Path(str(c.get("source", ""))).name
                for c in kept_chunks if c.get("source")
            }),
        }

def resolve_context_flag(cli_flag: Optional[bool]) -> bool:
    """CLI 旗標 > config（規格 §12 的 precedence）。"""
    if cli_flag is None:
        return bool(getattr(config_module, "KB_CONTEXT_GENERATE", False))
    return bool(cli_flag)


def human_revision_baseline(chunks, source: str) -> Dict[str, int]:
    """某份文件目前**已被人工確認**的 figure → revision（樂觀併發的基線）。

    只看 `human_verified` 的 structured chunk：那是唯一「使用者花時間對著原圖確認過」
    的資料，被覆蓋回機器結果就是無聲丟掉人工成果。
    """
    baseline: Dict[str, int] = {}
    for chunk in chunks or []:
        if not isinstance(chunk, dict) or not chunk.get("structured"):
            continue
        if chunk.get("source") != source:
            continue
        if chunk.get("verification_status") != "human_verified":
            continue
        figure_id = chunk.get("figure_id")
        if not isinstance(figure_id, str) or not figure_id:
            continue
        try:
            revision = int(chunk.get("revision", 0))
        except (TypeError, ValueError):
            revision = 0
        baseline[figure_id] = max(baseline.get(figure_id, 0), revision)
    return baseline


def _assert_figure_guard(guard: Optional[Dict], kb: Dict) -> None:
    """exclusive lock 內的最後一道：來源身分與人工修正基線都還成立嗎？

    兩件事只能在鎖內驗，而且必須在寫回之前：

    1. **來源身分**：抽取期間（`to_markdown` → 圖面 render → embedding，可能是幾十
       分鐘）來源檔可能被換掉。文字 chunk 來自舊版、structured figure 可能來自新版，
       混進同一份 KB 就是無法察覺的版本錯配。
    2. **人工修正的樂觀併發檢查**：carry-over 是在鎖外讀的。讀到 revision 3 之後、
       寫回之前，另一個 `review_figures fix` 可能已經提交 revision 4；這裡不擋的話
       我們會用 revision 3 覆蓋它，revision 倒退、人工成果無聲消失（§15.7 的單調性
       在並行情境下的同一條要求）。

    任一不成立 → raise，`save_knowledge_base` 不會被呼叫，整份零寫入。
    """
    if not guard:
        return
    fx = _figure_extract()
    source_path = guard.get("source_path")
    expected_id = guard.get("document_id")
    if source_path and expected_id:
        _assert_source_identity(fx, str(guard.get("source")), source_path, guard["root"],
                                expected_id, stage="提交前")

    baseline = guard.get("human_baseline")
    if baseline is None:
        return
    current_baseline = human_revision_baseline(kb.get("chunks"), guard.get("source"))
    if current_baseline != baseline:
        raise fx.FigureExtractionError(
            f"{guard.get('source')}: 併發衝突——這份文件的人工確認在本次入庫期間變了"
            f"（讀到 {sorted(baseline.items())}，現在是 {sorted(current_baseline.items())}）。"
            "繼續寫入會用舊 revision 蓋掉別人剛完成的人工修正，整份零寫入；請重跑一次。")


def _file_location(path) -> str:
    """檔案來源的位置字串：解析過 symlink 的絕對路徑。

    用解析後的路徑（而不是使用者打的字串）才不會把 `./spec.pdf`、
    `~/docs/spec.pdf`、以及指向同一個檔的 symlink 判成三份不同的文件。
    """
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(Path(path).absolute())


_UNFIXABLE_ARTIFACT_WARNINGS = frozenset({
    "artifact_unavailable", "artifact_missing_figure", "artifact_unreadable",
})


def _summary_item(entry: Dict) -> Dict:
    """覆核清單的一列 → 摘要行的一個元素（只有身分，零內容）。"""
    return {
        "page": int(entry.get("page") or 0),
        "figure_index": int(entry.get("figure_index") or 0),
        "figure_id": str(entry.get("figure_id") or ""),
        "kind": str(entry.get("kind") or ""),
    }


def _unfixable_reason(entry: Dict) -> str:
    """為什麼這一張沒辦法就地覆核（值域凍結成三個 slug）。"""
    warnings = {str(name) for name in (entry.get("warnings") or [])}
    if warnings & _UNFIXABLE_ARTIFACT_WARNINGS:
        return "artifact_missing"
    if entry.get("payload") is None or entry.get("payload_error"):
        return "payload_unreadable"
    return "not_fixable"


def _fallback_status_counts(committed_chunks) -> Dict:
    """覆核清單讀不到時的退路：直接數 chunk 的 `verification_status`。

    仍然以 **figure 為單位**（同一張圖被切成多個 chunk 只算一次），取最差的那個
    狀態——與 `aggregate_status` 同一條規則，不然多切幾刀的表就會被高報成可信。
    """
    grouped: Dict[str, List[str]] = {}
    for index, chunk in enumerate(committed_chunks or []):
        if not isinstance(chunk, dict) or not chunk.get("structured"):
            continue
        status = str(chunk.get("verification_status") or "")
        if not status:
            continue
        key = str(chunk.get("figure_id") or "") or f"#{index}"
        grouped.setdefault(key, []).append(status)
    if not grouped:
        # 六個非 PDF 入口（截圖 / 網頁 / 圖片…）永遠走這裡：沒有 structured
        # chunk 就不需要把 figure lane 的模組拉進來。
        return {}
    ranks = getattr(_figure_extract(), "VERIFICATION_RANK", {})
    worst = {key: min(statuses, key=lambda name: ranks.get(name, -1))
             for key, statuses in grouped.items()}
    counts: Dict[str, int] = {}
    for status in worst.values():
        counts[status] = counts.get(status, 0) + 1
    return counts


def _finish_summary_payload(payload: Dict) -> Dict:
    """補上精確 total、把三個 list 截到協定上限。

    `ingest_notify.format_summary_line` 是**逐字**序列化（寫出去的就是這份 dict），
    所以「形狀完整」與「行不會無限長」都是產生端的責任：total 一定是截斷**之前**
    的精確數量，list 才是被砍過的那一段。
    """
    for list_key, total_key in (("review", "review_total"),
                                ("unfixable", "unfixable_total"),
                                ("failed", "failed_total"),
                                (ingest_notify.ABSENT_KEY,
                                 ingest_notify.ABSENT_TOTAL_KEY)):
        items = payload.get(list_key) or []
        if list_key == ingest_notify.ABSENT_KEY:
            # 讀端要到 `render_action_block` 才過濾 actionable，所以**這裡先排**：
            # 一份 datasheet 很容易有上百筆「這塊不是結構化圖面」，照原順序截到 50
            # 筆的話，旋轉頁正文抽不出來這種真的要處理的那一筆就永遠到不了父行程。
            # 穩定分割，兩邊各自維持原本的頁序。
            items = ([item for item in items
                      if ingest_notify.is_actionable_absent(item.get("reason"))]
                     + [item for item in items
                        if not ingest_notify.is_actionable_absent(item.get("reason"))])
        payload[total_key] = len(items)
        payload[list_key] = items[:ingest_notify.MAX_PAYLOAD_ITEMS]
    return payload


def _fallback_review_items(committed_chunks) -> List[Dict]:
    """覆核清單讀不到時，直接從已提交的 chunk 挑出待覆核的圖（以 figure 為單位）。

    判不出 `fixable`，所以 `needs_review` 一律當「可覆核」列出：那一類本來就有
    下一步（去看原圖、修正）。`unverified` / `legacy_unverified` 則**不列** ——
    它們沒有可執行的下一步，列出來只是假警報（契約 §2.3）。
    """
    fx = _figure_extract()
    # **只有 `needs_review`**。`FLAGGED_VERIFICATION` 還含 `unverified` /
    # `legacy_unverified` —— 契約 §2.3 明訂那兩種不列、不提:它們沒有「使用者
    # 該做什麼」可講,列出來只是把每次 ingest 都變成一則假警報,然後使用者學會
    # 忽略整個通知。「寧可多叫」在這裡是錯的,因為多叫的那些沒有下一步。
    needs_review = getattr(fx, "VERIF_NEEDS_REVIEW", "needs_review")
    seen: Dict[str, Dict] = {}
    for chunk in committed_chunks or []:
        if not isinstance(chunk, dict) or not chunk.get("structured"):
            continue
        if str(chunk.get("verification_status") or "") != needs_review:
            continue
        figure_id = str(chunk.get("figure_id") or "")
        if not figure_id or figure_id in seen:
            continue
        seen[figure_id] = {
            "page": int(chunk.get("page") or 0),
            "figure_index": int(chunk.get("figure_index") or 0),
            "figure_id": figure_id,
            "kind": str(chunk.get("figure_kind") or ""),
        }
    return [seen[key] for key in sorted(seen)]


def _ingest_summary_line(document: ExtractedDocument, committed_chunks,
                         figure_guard: Optional[Dict]) -> str:
    """本次 ingest 的單行機器可讀摘要（`ingest_notify` 的 payload 契約 §2.1）。

    父行程（MCP server）拿得到的只有這條子行程的 stdout，所以「這一次發生了
    什麼」必須由知情的這一端講。父行程自己去掃 KB 的話會把**別次 run**、甚至
    別份文件的舊帳算進來——舊 run 的失敗每次 ingest 都重報一次，通知就廢了。

    資料來源刻意用 `list_figures`（`review_figures(action="list")` 看到的同一份），
    不另外自己掃 manifest：兩份實作一漂移，通知說有 3 張待覆核、工具列出 5 張。
    """
    guard = figure_guard or {}
    root = str(guard.get("root") or "")
    document_id = str(guard.get("document_id") or "")
    run_id = str(guard.get("run_id") or "")
    payload: Dict = {
        "schema": ingest_notify.SUMMARY_SCHEMA,
        "document": document.source,
        "document_id": document_id,
        "run_id": run_id,
        "status_counts": {},
        "review": [], "unfixable": [], "failed": [],
        # 缺席清單走抽取端掛上來的那一份：提交點掃 KB 是看不到「沒進 KB 的東西」的。
        ingest_notify.ABSENT_KEY: [
            {"page": int(item.get("page") or 0),
             "bbox": list(item["bbox"]) if item.get("bbox") else None,
             "channel": str(item.get("channel") or ""),
             "reason": str(item.get("reason") or "")}
            for item in (getattr(document, _ABSENT_ATTR, None) or [])
        ],
    }

    entries = None
    if root and document_id:
        try:
            entries = _figure_extract().list_figures(
                root, committed_chunks, document_id=document_id)
        except Exception as exc:  # noqa: BLE001 — KB 已提交，摘要只能降級不能失敗
            print(f"[WARN] 讀不到覆核清單（{exc}）；本次摘要只報抽取失敗的圖。")
            entries = None

    if entries is None:
        # 退路：guard 自己記下來的失敗清單 ＋ chunk 級狀態統計。
        payload["failed"] = [dict(item) for item in (guard.get("failed") or [])]
        payload["status_counts"] = _fallback_status_counts(committed_chunks)
        # **待覆核也要列出來**。只留 status_counts 的話，一次成功入庫、而且有
        # needs_review 圖的 ingest 會回 `status: ok`、plugin 也不通知 —— 使用者
        # 無聲漏掉必要的覆核，那正是這條通知鏈存在的理由。
        # 這條路徑讀不到覆核清單，所以判不出 fixable：一律當成「可覆核」列出。
        # 寧可多叫一次，也不要漏掉一張。
        payload["review"] = _fallback_review_items(committed_chunks)
        return ingest_notify.format_summary_line(_finish_summary_payload(payload))

    fx = _figure_extract()
    counts: Dict[str, int] = {}
    for entry in entries:
        status = str(entry.get("verification_status") or "")
        item = _summary_item(entry)
        if not entry.get("in_kb"):
            if str(entry.get("extraction_status") or "") == fx.EXTRACTION_SKIPPED:
                # 分類器判定「不是圖面」（封面 / logo / 照片）：同樣沒進 KB，但它
                # **不是抽取失敗**，也沒有任何下一步。報成失敗的話，每一份 datasheet
                # 的通知都會掛著幾筆假警報，真的抽壞的那一張就淹在裡面。這一類只
                # 出現在缺席帳（`absent`）與 review artifact 的「判定不是圖面」一節。
                continue
            # **只算本次 run**：artifacts 裡還躺著以前那幾次的失敗，全算進來的話
            # 每次 ingest 都會重報同一批舊帳（「舊 run 零誤報」）。
            if not run_id or str(entry.get("run_id") or "") != run_id:
                continue
            item["reason"] = _failure_reason_slug(entry.get("reasons"))
            payload["failed"].append(item)
            continue
        if status:
            counts[status] = counts.get(status, 0) + 1
        # **只有 `needs_review` 會進通知**（契約 §2.3）。`unverified` /
        # `legacy_unverified` 即使 artifact 壞掉也不列：它們本來就沒有「使用者
        # 該做什麼」——叫人 remove + 重灌，重灌完多半還是 unverified、artifact
        # 還是那樣，是一條**不會收斂**的指示。這也讓正常路徑與 fallback 路徑
        # 給出同一套答案（兩邊不一致比兩邊都保守更糟）。
        if status != fx.VERIF_NEEDS_REVIEW:
            continue
        if (not entry.get("fixable") or entry.get("payload_error")
                or entry.get("payload") is None):
            # 看不到 payload / 原圖就沒有可覆核的證據，`action="fix"` 幫不上忙；
            # 唯一的路是 remove + 重灌。
            item["reason"] = _unfixable_reason(entry)
            payload["unfixable"].append(item)
        else:
            payload["review"].append(item)
    payload["status_counts"] = counts
    return ingest_notify.format_summary_line(_finish_summary_payload(payload))


def _commit_document_to_kb(
    document: ExtractedDocument,
    output_file: str,
    *,
    label: str = "文件",
    generate_context: bool = False,
    figure_guard: Optional[Dict] = None,
    fresh: bool = False,
    source_location: str = "",
) -> bool:
    """把一份 ExtractedDocument 併進知識庫（同名文件先移除舊 chunks）。

    `source_location` 是這份文件**來自哪裡**（解析後的絕對路徑，或 URL）。
    刻意與 figure lane 的 `source_identity`（整檔 sha256，用來抓「抽取途中檔案被
    換掉」）區分開：那是內容身分，同一個檔改過內容就會變；這裡要的是位置身分，
    否則正常的「改了再灌一次」會被誤判成撞名。
    KB 用 basename 當文件識別，所以 `a/spec.pdf` 與 `b/spec.pdf` 在裡面是同一個
    身分；沒有這個欄位的話，後灌的那份會把前一份整份換掉，訊息還跟正常更新
    一字不差。給了身分就會在寫入前比對，對不上一律 `DocumentIdentityConflict`
    且零寫入。給空字串維持舊語意（不擋、不記）。

    `fresh=True`：這一份文件成為新 KB 的全部內容——既有 chunks 在**同一次原子
    提交**裡被換掉，舊向量隨著 generation 換新自動失效。fresh **不會**為了 reset
    去清 `.codetrail/figures/`（ingest 本來就會寫入這一次的 run、提交後也可能依
    retention 回收沒被引用的舊 run，那與 fresh 無關）；
    這份文件自己的 `human_verified` 已經在抽取階段沿用回來了（§15.7）。
    中途任何一步失敗都不會清空 KB：清空只發生在 exclusive lock 內、提交之前，
    而提交失敗會整批回滾。

    七個入口以前各自複製這段（載入 → 去重同名 → embedding → 配 id → append →
    save）。共用之後「一份文件怎麼進 KB」只有一條路；要在入庫前多做一步
    （例如生成 chunk 脈絡）也只有一個掛點，不會漏掉某個入口。

    **順序是刻意的**：脈絡生成與 embedding 都不需要現有 KB，所以全部在鎖外做完；
    只有「載入 → 併入 → 寫回」進同一把 exclusive store lock。以前是先載入、再花
    幾十分鐘生成、最後拿那份過期快照覆寫——中間有人入庫的文件會整份消失
    （lost update）。原子的 JSON/NPZ 提交只能防半套檔案，防不了這個。

    回傳 False 代表沒有內容可入庫（呼叫端決定 exit 還是 return）。
    """
    output_path = Path(output_file)
    new_chunks = document.chunks
    if not new_chunks:
        print("[WARN] 沒有提取到任何內容")
        return False

    print(f"[INFO] 提取 {len(new_chunks)} 個文字區塊")

    # chunk 級生成脈絡（預設關閉）。失敗一律往上丟：這一步失敗就不該發布，
    # 覆蓋率不足也一樣——寧可整批不進 KB，也不要寫出半套脈絡的知識庫。
    if generate_context:
        import context_generation

        report = context_generation.generate_document_context(
            document, kb_path=output_path
        )
        print(report.format_summary())

    # 生成 embeddings
    needs_gate = context_signals.has_any_ctx(new_chunks)
    print(f"[INFO] 使用 {EMBEDDING_MODEL} 生成 embeddings...")
    new_chunks = generate_embeddings(new_chunks, with_gate=needs_gate)

    # 為每個 chunk 生成唯一 ID（格式的唯一定義在 knowledge_store.chunk_id；
    # 這裡以前有一份行內複製，兩份實作一漂移，人工 fix 換掉的 chunk 就對不上舊 id）
    for chunk in new_chunks:
        chunk['id'] = chunk_id(chunk)

    # 這裡開始才碰共用狀態：整段 read-modify-write 在同一把鎖內。
    with knowledge_store_lock(output_path, exclusive=True):
        # fresh 只需要舊 KB 的 chunk metadata（figure guard + 清掉幾個的計數），
        # 那些向量下一行就要被丟掉了，沒有理由先把它們還原（甚至重算）出來。
        kb = load_knowledge_base(output_path, _already_locked=True,
                                 _restore_vectors=not fresh)
        # figure guard 必須看**清空之前**的 KB：它比對的是「這份文件的人工確認在
        # 抽取期間有沒有被別人改掉」，看空的當然永遠一致。
        _assert_figure_guard(figure_guard, kb)

        carried_human = 0
        dropped_names: list = []
        if fresh:
            dropped_chunks = len(kb.get("chunks", []))
            dropped_names = [str(name) for name in kb["metadata"].get("documents", [])]
            dropped_docs = len(dropped_names)
            carried_human = len(human_revision_baseline(new_chunks, document.source))
            print(f"[INFO] fresh ingest：清空既有 {dropped_chunks} 個 chunk"
                  f"（{dropped_docs} 份文件），舊 embeddings cache 一併失效")
            kb["chunks"] = []
            kb["metadata"]["documents"] = []
            # fresh 是「這一份就是新 KB 的全部」：舊的身分紀錄留著會讓
            # 下一步的比對拿已經不存在的文件當對照組。
            kb["metadata"].pop("document_sources", None)

        # 檢查是否已存在同名文件（若有則先移除舊的）
        doc_name = document.source
        # 撞名但來源不同一律在這裡 fail-loud——**還沒動到 kb**，所以是零寫入。
        identity_warning = check_document_identity(
            kb["metadata"], doc_name, source_location
        )
        if identity_warning:
            print(f"[WARN] {identity_warning}")
        if doc_name in kb["metadata"]["documents"]:
            print(f"[INFO] 更新現有{label}: {doc_name}")
            kb["chunks"] = [c for c in kb["chunks"] if c["source"] != doc_name]
            kb["metadata"]["documents"].remove(doc_name)
        else:
            print(f"[INFO] 新增{label}: {doc_name}")

        # Append 到知識庫
        kb["chunks"].extend(new_chunks)
        kb["metadata"]["documents"].append(doc_name)
        record_document_identity(kb["metadata"], doc_name, source_location)

        # 儲存
        save_knowledge_base(kb, output_path, _already_locked=True)
        if fresh:
            print(f"[INFO] fresh ingest 已重建 KB，本文件沿用 {carried_human} 筆 "
                  "human_verified。")
            if dropped_docs > 1 or (dropped_docs == 1 and doc_name not in dropped_names):
                print("[WARN] 被移出 KB 的其他文件：artifact 檔案（.codetrail/figures/）"
                      "都保留著，但它們的人工確認**不會**在重新 ingest 時自動恢復"
                      "（沿用需要該 figure 仍在 KB 內，見 _existing_human_entries）。")

    # KB-aware prune 一律在 store lock 釋放之後：prune 自己要重讀 KB，在鎖內呼叫
    # 會自鎖。失敗只警告——KB 已經成功提交，舊 run 目錄留著只是佔空間。
    if figure_guard and figure_guard.get("wrote_run"):
        try:
            _figure_extract().prune_old_runs(
                figure_guard["root"],
                document_id=figure_guard["document_id"],
                kb_path=output_path,
            )
        except Exception as exc:  # noqa: BLE001 — 清理失敗不得回頭影響已提交的 KB
            print(f"[WARN] figure review artifacts 清理失敗（KB 已成功寫入）: {exc}")

    # 摘要是這次 ingest 的**唯一**機器可讀出口，而且只在提交成功之後才印：
    # 失敗路徑（回 False / raise）印了就是宣稱一次沒發生的入庫。放在 prune
    # 之後是為了讓清掉的舊 run 不再出現在覆核清單裡。
    try:
        print(_ingest_summary_line(document, new_chunks, figure_guard), flush=True)
    except Exception as exc:  # noqa: BLE001 — KB 已提交，摘要算不出來也不能倒退成失敗
        print(f"[WARN] 本次 ingest 摘要產生失敗（KB 已成功寫入）: {exc}")
    return True


def add_document(input_file: str, output_file: str, *, generate_context: bool = False,
                 preflight_only: bool = False, fresh: bool = False):
    """將文件加入知識庫

    `generate_context` 只有 `rebuild` 子命令會給 True——chunk 脈絡的唯一執行路徑
    是同步 CLI rebuild（MCP 那條有 600 秒 timeout，數十個大窗串行必然超時）。

    `preflight_only=True` 只算 PDF figure 預算並印報告，**零寫入**：不碰
    knowledge.json / embeddings cache、不呼叫 VL、不算 embedding（契約 §11.4）。

    `fresh=True` 是「一步到位重建」：清空既有 chunks、讓舊 embeddings cache 失效、
    只留這一份文件，全部在同一次原子提交裡完成。它**不會為了 reset 去清**
    `.codetrail/figures/`；同一份文件的人工修正照 §15.7 沿用，被移出 KB 的其他文件
    則是「檔案留著、但重新 ingest 不會自動恢復人工確認」（見 `_commit_document_to_kb`）。
    預設為合併語意：新 basename 加入，同一來源的同 basename 原子替換舊 chunks；
    不同來源的同名文件仍由身分閘拒絕。
    """
    input_path = Path(input_file)
    output_path = Path(output_file)

    # 檢查輸入檔案
    if not input_path.exists():
        print(f"[ERROR] 檔案不存在: {input_file}")
        sys.exit(1)

    allowed = SUPPORTED_EXTENSIONS | BINARY_EXTENSIONS | ELF_EXTENSIONS
    if input_path.suffix.lower() not in allowed:
        print(f"[ERROR] 不支援的檔案類型: {input_path.suffix}")
        print(f"        文字: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        print(f"        二進位: {', '.join(sorted(BINARY_EXTENSIONS))}")
        print(f"        ELF: {', '.join(sorted(ELF_EXTENSIONS))}")
        sys.exit(1)

    if preflight_only and fresh:
        print("[ERROR] --preflight 是零寫入的估算，不能同時 --fresh（那是重建 KB）")
        sys.exit(1)

    if preflight_only:
        # 零寫入的保證要含「不去碰 KB」：load_knowledge_base 會建立 store lock 檔，
        # 所以 preflight 分支刻意排在它之前。
        if input_path.suffix.lower() != ".pdf":
            print(f"[ERROR] --preflight 只適用 .pdf（你給的是 {input_path.suffix}）")
            sys.exit(1)
        print(f"[INFO] 處理: {input_path.name}")
        _extract_pdf_document_impl(str(input_path), preflight_only=True,
                                   root=None, kb_path=None)
        print("[INFO] --preflight：只計算 figure 預算，未寫入知識庫（零寫入）。")
        return

    # 先驗一次：壞掉的 KB 要在付出抽取／生成成本前就 fail。真正併入用的快照
    # 是 _commit_document_to_kb 在鎖裡重新載的那一份。非 fresh 時這一步同時把
    # embeddings cache 補好（重算在鎖外做，不會卡住整個 KB）。
    load_knowledge_base(output_path, _quiet=True, _restore_vectors=not fresh)

    # 處理新文件
    print(f"[INFO] 處理: {input_path.name}")
    document = process_file_document(str(input_path), kb_path=output_file)

    if not _commit_document_to_kb(
        document, output_file, label="文件", generate_context=generate_context,
        figure_guard=getattr(document, _FIGURE_PRUNE_ATTR, None), fresh=fresh,
        source_location=_file_location(input_path),
    ):
        sys.exit(1)

# ============================================================
# 互動式確認函式
# ============================================================
def ask_yes_no(prompt: str, default: bool = True) -> bool:
    """詢問使用者 yes/no 問題"""
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        response = input(prompt + suffix).strip().lower()
        if not response:
            return default
        if response in ('y', 'yes', '是'):
            return True
        if response in ('n', 'no', '否'):
            return False
        print("請輸入 y 或 n")


def ask_output_file(default: str = "knowledge.json") -> str:
    """詢問使用者輸出檔案路徑"""
    response = input(f"請輸入知識庫檔案路徑 [{default}]: ").strip()
    return response if response else default


# ============================================================
# 聊天截圖模式（互動式）
# ============================================================
def interactive_chat_screenshot(image_file: str, output_file: str):
    """
    互動式處理聊天截圖：
    1. 分析並顯示結果
    2. 詢問是否加入知識庫
    3. 若是，入庫；若否，結束
    """
    image_path = Path(image_file)

    # 檢查輸入檔案
    if not image_path.exists():
        print(f"[ERROR] 檔案不存在: {image_file}")
        sys.exit(1)

    if image_path.suffix.lower() not in IMAGE_EXTENSIONS:
        print(f"[ERROR] 不支援的圖片類型: {image_path.suffix}")
        print(f"        支援: {', '.join(IMAGE_EXTENSIONS)}")
        sys.exit(1)

    # 分析截圖
    print(f"[INFO] 使用 VL 模型分析截圖: {image_path.name}")
    content = extract_chat_from_screenshot(str(image_path))

    if not content:
        print("[ERROR] VL 模型分析失敗")
        sys.exit(1)

    # 顯示完整結果
    print(f"\n[INFO] 分析完成，內容長度: {len(content)} 字元")
    print("=" * 60)
    print(content)
    print("=" * 60)

    # 詢問是否加入知識庫
    print()
    if ask_yes_no(f"是否將此內容加入 {output_file}？"):
        _add_chat_content_to_kb(image_path, content, output_file)
    else:
        print("[INFO] 已取消，內容未儲存")


def _add_chat_content_to_kb(image_path: Path, content: str, output_file: str,
                            *, fresh: bool = False):
    """將已分析的聊天內容加入知識庫（內部函式）"""
    # 自動快取分析結果
    cache_file = _save_to_cache(image_path.name, content, "chat")
    if cache_file:
        print(f"[INFO] 快取已存: {cache_file}")

    document = build_chat_document(image_path.name, content)
    _commit_document_to_kb(document, output_file, label="截圖知識", fresh=fresh,
                           source_location=_file_location(image_path))


def add_chat_screenshot(image_file: str, output_file: str, *, fresh: bool = False):
    """將聊天截圖加入知識庫（相容舊 API，直接入庫不詢問）"""
    image_path = Path(image_file)
    output_path = Path(output_file)

    # 檢查輸入檔案
    if not image_path.exists():
        print(f"[ERROR] 檔案不存在: {image_file}")
        sys.exit(1)

    if image_path.suffix.lower() not in IMAGE_EXTENSIONS:
        print(f"[ERROR] 不支援的圖片類型: {image_path.suffix}")
        print(f"        支援: {', '.join(IMAGE_EXTENSIONS)}")
        sys.exit(1)

    # 先驗一次：壞掉的 KB 要在付出 VL 成本前就 fail
    load_knowledge_base(output_path, _quiet=True)

    # 處理截圖
    print(f"[INFO] 處理: {image_path.name}")
    document = process_chat_screenshot_document(str(image_path))

    if not _commit_document_to_kb(document, output_file, label="截圖知識", fresh=fresh,
                                  source_location=_file_location(image_path)):
        sys.exit(1)


# ============================================================
# 網頁處理
# ============================================================
def fetch_url_content(url: str) -> tuple[str, str]:
    """
    抓取網頁內容並轉換成 Markdown

    Returns: (content, title) 或 ("", "") 如果失敗
    """
    import requests

    # 檢查是否有 html2text
    try:
        import html2text
    except ImportError:
        print("[ERROR] 需要安裝 html2text 套件")
        print("請執行: pip install html2text")
        return "", ""

    print(f"[INFO] 正在連線: {url}")

    # 設定 headers 模擬瀏覽器
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        print(f"[ERROR] 無法連線到 {url}")
        print("        請檢查網路連線或網址是否正確")
        return "", ""
    except requests.exceptions.Timeout:
        print(f"[ERROR] 連線逾時: {url}")
        return "", ""
    except requests.exceptions.HTTPError as e:
        print(f"[ERROR] HTTP 錯誤: {e}")
        return "", ""
    except Exception as e:
        print(f"[ERROR] 抓取失敗: {e}")
        return "", ""

    # 處理編碼
    response.encoding = response.apparent_encoding or 'utf-8'
    html_content = response.text

    # 提取標題
    title = ""
    title_match = re.search(r'<title[^>]*>([^<]+)</title>', html_content, re.IGNORECASE)
    if title_match:
        title = title_match.group(1).strip()

    # 轉換成 Markdown
    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = True  # 忽略圖片
    h.ignore_emphasis = False
    h.body_width = 0  # 不換行
    h.unicode_snob = True
    h.skip_internal_links = True

    markdown_content = h.handle(html_content)

    # 清理內容
    markdown_content = clean_markdown_content(markdown_content)

    return markdown_content, title


def clean_markdown_content(content: str) -> str:
    """清理 Markdown 內容，移除雜訊"""
    lines = content.split('\n')
    cleaned_lines = []

    # 常見的導航/頁尾關鍵字
    skip_patterns = [
        r'^(Skip to|跳到|跳至|導航|Navigation|Menu|選單)',
        r'^(Copyright|©|版權|All rights reserved)',
        r'^(Privacy|隱私|Terms|條款)',
        r'^\[.*\]\(javascript:',  # JavaScript 連結
        r'^(\s*\|\s*)+$',  # 空表格行
    ]

    skip_section = False
    empty_count = 0

    for line in lines:
        stripped = line.strip()

        # 跳過空行堆積
        if not stripped:
            empty_count += 1
            if empty_count <= 2:  # 最多保留 2 個連續空行
                cleaned_lines.append(line)
            continue
        else:
            empty_count = 0

        # 跳過匹配的雜訊
        should_skip = False
        for pattern in skip_patterns:
            if re.match(pattern, stripped, re.IGNORECASE):
                should_skip = True
                break

        if should_skip:
            continue

        # 跳過過短的行（可能是導航按鈕等）
        if len(stripped) < 3 and not stripped.startswith('#'):
            continue

        cleaned_lines.append(line)

    return '\n'.join(cleaned_lines).strip()


def generate_url_name(url: str) -> str:
    """
    從 URL 生成唯一的名稱（避免撞名）
    使用 {netloc}_{last_path} 格式，避免撞名
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)

    # 清理 netloc（移除 www. 和特殊字元）
    netloc = parsed.netloc.replace('www.', '').replace('.', '_').replace(':', '_')

    # 取 path 最後一段
    path_parts = [p for p in parsed.path.split('/') if p]
    if path_parts:
        last_path = path_parts[-1]
        # 清理特殊字元
        last_path = re.sub(r'[^\w\-]', '_', last_path)
        return f"{netloc}_{last_path}"
    else:
        return netloc


def build_url_document(
    url: str, content: str, title: str, fetched_at: str
) -> ExtractedDocument:
    """抓回來的網頁 Markdown → ExtractedDocument（互動式與自動入庫共用）"""
    url_name = generate_url_name(url)
    return build_text_document(
        content,
        source=f"url_{url_name}",
        base_type='web',
        doc_type='web',
        extra={
            "origin": "url",
            "url": url,              # 保留原始 URL
            "title": title,          # 補存標題
            "fetched_at": fetched_at,  # 補存抓取時間
        },
    )


def process_url_document(url: str) -> Optional[ExtractedDocument]:
    """處理網頁 URL，提取內容並整理成知識區塊

    Returns:
        成功: ExtractedDocument
        失敗: None
    """
    content, title = fetch_url_content(url)
    fetched_at = datetime.now().isoformat()  # 記錄抓取時間

    if not content:
        return None

    print(f"[INFO] 網頁標題: {title or '(無標題)'}")
    print(f"[INFO] 提取完成，內容長度: {len(content)} 字元")
    print("-" * 40)
    print(content[:500] + "..." if len(content) > 500 else content)
    print("-" * 40)

    return build_url_document(url, content, title, fetched_at)


def process_url(url: str) -> Optional[Tuple[List[Dict], str]]:
    """處理網頁 URL（只要 (chunks, url_name) 的相容入口）"""
    document = process_url_document(url)
    if document is None:
        return None
    # source 是 "url_<name>"，去掉前綴還原 url_name
    return document.chunks, document.source[len("url_"):]


# ============================================================
# 網頁模式（互動式）
# ============================================================
def interactive_url(url: str, output_file: str):
    """
    互動式處理網頁：
    1. 抓取並顯示結果
    2. 詢問是否加入知識庫
    3. 若是，入庫；若否，結束
    """
    # 簡單驗證 URL 格式
    if not url.startswith(('http://', 'https://')):
        print(f"[ERROR] 無效的 URL: {url}")
        print("        URL 必須以 http:// 或 https:// 開頭")
        sys.exit(1)

    # 抓取網頁
    print(f"[INFO] 正在抓取網頁: {url}")
    content, title = fetch_url_content(url)

    if not content:
        print("[ERROR] 網頁抓取失敗")
        sys.exit(1)

    # 顯示完整結果
    print(f"\n[INFO] 網頁標題: {title or '(無標題)'}")
    print(f"[INFO] 抓取完成，內容長度: {len(content)} 字元")
    print("=" * 60)
    print(content)
    print("=" * 60)

    # 詢問是否加入知識庫
    print()
    if ask_yes_no(f"是否將此內容加入 {output_file}？"):
        _add_url_content_to_kb(url, content, title, output_file)
    else:
        print("[INFO] 已取消，內容未儲存")


def _add_url_content_to_kb(url: str, content: str, title: str, output_file: str):
    """將已抓取的網頁內容加入知識庫（內部函式）"""
    fetched_at = datetime.now().isoformat()

    # 自動快取抓取結果
    cache_file = _save_to_cache(url, content, "url", {"title": title})
    if cache_file:
        print(f"[INFO] 快取已存: {cache_file}")

    document = build_url_document(url, content, title, fetched_at)
    _commit_document_to_kb(document, output_file, label="網頁知識",
                           source_location=url)


def add_url(url: str, output_file: str, *, fresh: bool = False):
    """將網頁內容加入知識庫（相容舊 API，直接入庫不詢問）"""
    output_path = Path(output_file)

    # 簡單驗證 URL 格式
    if not url.startswith(('http://', 'https://')):
        print(f"[ERROR] 無效的 URL: {url}")
        print("        URL 必須以 http:// 或 https:// 開頭")
        sys.exit(1)

    # 先驗一次：壞掉的 KB 要在抓網頁前就 fail
    load_knowledge_base(output_path, _quiet=True)

    # 處理網頁
    document = process_url_document(url)

    if document is None:
        print("[ERROR] 無法從網頁提取內容，新增失敗")
        sys.exit(1)

    _commit_document_to_kb(document, output_file, label="網頁知識", fresh=fresh,
                           source_location=url)


# ============================================================
# 技術圖片模式（互動式）
# ============================================================
def interactive_technical_image(image_file: str, output_file: str):
    """
    互動式處理技術圖片：
    1. 分析並顯示結果
    2. 詢問是否加入知識庫
    3. 若是，入庫；若否，結束
    """
    image_path = Path(image_file)

    # 檢查輸入檔案
    if not image_path.exists():
        print(f"[ERROR] 檔案不存在: {image_file}")
        sys.exit(1)

    if image_path.suffix.lower() not in IMAGE_EXTENSIONS:
        print(f"[ERROR] 不支援的圖片類型: {image_path.suffix}")
        print(f"        支援: {', '.join(IMAGE_EXTENSIONS)}")
        sys.exit(1)

    # 分析圖片
    print(f"[INFO] 使用 VL 模型分析技術圖片: {image_path.name}")
    content = extract_info_from_image(str(image_path))

    if not content:
        print("[ERROR] VL 模型分析失敗")
        sys.exit(1)

    # 顯示完整結果
    print(f"\n[INFO] 分析完成，內容長度: {len(content)} 字元")
    print("=" * 60)
    print(content)
    print("=" * 60)

    # 詢問是否加入知識庫
    print()
    if ask_yes_no(f"是否將此內容加入 {output_file}？"):
        _add_image_content_to_kb(image_path, content, output_file)
    else:
        print("[INFO] 已取消，內容未儲存")


def _add_image_content_to_kb(image_path: Path, content: str, output_file: str):
    """將已分析的技術圖片內容加入知識庫（內部函式）"""
    # 自動快取分析結果
    cache_file = _save_to_cache(image_path.name, content, "image")
    if cache_file:
        print(f"[INFO] 快取已存: {cache_file}")

    document = build_image_document(image_path.name, content)
    _commit_document_to_kb(document, output_file, label="圖片知識",
                           source_location=_file_location(image_path))


def add_technical_image(image_file: str, output_file: str, *, fresh: bool = False):
    """將技術圖片加入知識庫（相容舊 API，直接入庫不詢問）"""
    image_path = Path(image_file)
    output_path = Path(output_file)

    # 檢查輸入檔案
    if not image_path.exists():
        print(f"[ERROR] 檔案不存在: {image_file}")
        sys.exit(1)

    if image_path.suffix.lower() not in IMAGE_EXTENSIONS:
        print(f"[ERROR] 不支援的圖片類型: {image_path.suffix}")
        print(f"        支援: {', '.join(IMAGE_EXTENSIONS)}")
        sys.exit(1)

    # 先驗一次：壞掉的 KB 要在付出 VL 成本前就 fail
    load_knowledge_base(output_path, _quiet=True)

    # 處理圖片
    print(f"[INFO] 處理: {image_path.name}")
    document = process_technical_image_document(str(image_path))

    if not _commit_document_to_kb(document, output_file, label="圖片知識", fresh=fresh,
                                  source_location=_file_location(image_path)):
        sys.exit(1)


# ============================================================
# 入口
# ============================================================
def rebuild_cli(argv: List[str]) -> int:
    """`python3 RAG.py rebuild ...`：chunk 脈絡的唯一執行路徑。

    刻意用 argparse 另開一個子命令，不去擴充下面那個手工 argv parser——旗標語意
    （互斥、precedence、同給即錯）交給 argparse，不要再手寫一套。
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="RAG.py rebuild",
        description=(
            "把文件灌進知識庫。這是唯一會生成 chunk 脈絡（contextual retrieval）"
            "的路徑；MCP 的 ingest_document 永遠不生成。"
        ),
    )
    parser.add_argument("--kb", required=True, help="knowledge.json 路徑（不存在會建立）")
    parser.add_argument(
        "documents", nargs="+", help="要灌的文件（pdf/md/txt/bin/elf）"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--context", dest="context", action="store_const", const=True,
        help="這次強制生成 chunk 脈絡（需要主 llama-server）",
    )
    group.add_argument(
        "--no-context", dest="context", action="store_const", const=False,
        help="這次強制不生成",
    )
    parser.add_argument(
        "--preflight", action="store_true",
        help="只算 PDF figure 預算並印報告，零寫入（exit 2 = 超出預算）",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help=("先清空既有 chunks 再灌（第一份文件生效，之後照常合併；同一來源 basename 更新）；"
              "embeddings cache 隨新 generation 自動失效，.codetrail/figures/ 不動"),
    )
    parser.set_defaults(context=None)
    args = parser.parse_args(argv)

    if args.preflight and args.fresh:
        parser.error("--preflight 是零寫入的估算，不能同時 --fresh")

    if args.preflight:
        print("[INFO] --preflight：只計算 figure 預算，不入庫、不生成 chunk 脈絡。")
        for document_path in args.documents:
            print(f"\n=== {document_path} ===")
            try:
                add_document(document_path, args.kb, preflight_only=True)
            except Exception as exc:  # noqa: BLE001 — 對映契約 §11.4 的 exit code
                code = _pdf_cli_error_code(exc)
                if code is None:
                    raise
                print(f"[ERROR] {exc}")
                return code
        return 0

    generate_context = resolve_context_flag(args.context)
    source = "CLI 旗標" if args.context is not None else "config"
    print(f"[INFO] chunk 脈絡生成: {'開' if generate_context else '關'}（來源: {source}）")

    fresh = args.fresh
    if fresh:
        print("[INFO] --fresh：第一份文件會清空既有知識庫（figure artifacts 不動），"
              "後續文件照常合併（同一來源 basename 更新）。")
    for document_path in args.documents:
        print(f"\n=== {document_path} ===")
        add_document(document_path, args.kb, generate_context=generate_context,
                     fresh=fresh)
        fresh = False   # 只清一次，否則每一份都會把前一份洗掉
    return 0


def print_usage():
    """印出使用說明"""
    print("用法:")
    print("  python3 RAG.py <input_file> <output_json>             # 一般文件（直接入庫）")
    print("  python3 RAG.py <input.pdf> <output_json> --preflight  # 只算 PDF figure 預算並印報告（零寫入）")
    print("  python3 RAG.py rebuild --kb <output_json> <input>... [--preflight|--fresh]  # 批次入庫（唯一會生成 chunk 脈絡的路徑）")
    print("  python3 RAG.py <input_file> <output_json> --fresh     # 清空既有 chunks 後只留這一份（figure artifacts 不動）")
    print("  python3 RAG.py <screenshot> <output_json> --chat      # 聊天截圖（互動式）")
    print("  python3 RAG.py <image> <output_json> --image          # 技術圖片（互動式）")
    print("  python3 RAG.py <url> <output_json> --url              # 網頁（互動式）")
    print("")
    print("互動式模式（--chat/--image/--url）會：")
    print("  1. 分析/抓取內容並顯示完整結果")
    print("  2. 詢問「是否將此內容加入 <output_json>？」")
    print("  3. 若是則入庫，若否則結束")
    print("")
    print("參數:")
    print("  input_file   要加入的文件 (pdf/md/txt/bin/elf/...)")
    print("  screenshot   聊天截圖圖片 (png/jpg/jpeg/gif/webp)")
    print("  image        技術圖片 (架構圖/流程圖/記憶體映射等)")
    print("  url          網頁 URL (http:// 或 https://)")
    print("  output_json  知識庫檔案 (不存在則建立，存在則合併；同一來源 basename 更新)")
    print("")
    print("範例:")
    print("  python3 RAG.py manual.pdf knowledge.json                       # PDF 直接入庫")
    print("  python3 RAG.py manual.pdf knowledge.json --preflight           # 只看 figure 預算（exit 2 = 超出）")
    print("  python3 RAG.py firmware.bin knowledge.json                     # binary/ELF 直接入庫")
    print("  python3 RAG.py teams_chat.png knowledge.json --chat            # 聊天截圖")
    print("  python3 RAG.py npx6_arch.png knowledge.json --image            # 技術圖片")
    print("  python3 RAG.py teams_chat.png knowledge.json --chat -y         # 同上但不問")
    print("  python3 RAG.py https://docs.example.com/guide knowledge.json --url  # 網頁")
    print("")
    print(f"支援的文字類型: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
    print(f"支援的圖片類型: {', '.join(sorted(IMAGE_EXTENSIONS))}")
    print(f"支援的二進位類型: {', '.join(sorted(BINARY_EXTENSIONS))}")
    print(f"支援的 ELF 類型: {', '.join(sorted(ELF_EXTENSIONS))}")


def _pdf_cli_error_code(exc: BaseException) -> Optional[int]:
    """PDF figure lane 的例外 → 契約 §11.4 凍結的 exit code；不是它的例外回 None。

    用 `sys.modules.get` 而不 import：非 PDF 模式（`--chat` / `--image` / `--url`）
    不該為了 catch 一個不可能發生的例外而把整條 figure 鏈拉進來。真的拋了
    `FigureBudgetError`，`figure_extract` 必然已經在 `sys.modules` 裡。
    """
    figure_extract = sys.modules.get("figure_extract")
    if figure_extract is not None and isinstance(exc, figure_extract.FigureBudgetError):
        return 2   # 超出預算：報告已完整印出
    if isinstance(exc, PdfPreflightUnavailable):
        return 1   # 產不出報告：不得以 exit 0 假成功
    return None


def main(argv: List[str]) -> int:
    """`__main__` 的實際內容（抽成函式才測得到 exit code；契約 §11.4）。

    `add_document` 內部的 `sys.exit(1)` 照舊直接往上拋 `SystemExit`——既有行為
    一個字都沒變，只有 figure lane 的兩種例外被映射成 2 / 1。
    """
    preflight_only = "--preflight" in argv
    fresh = "--fresh" in argv
    auto_yes = any(arg in ("-y", "--yes") for arg in argv)
    args = [arg for arg in argv
            if arg not in ("-y", "--yes", "--preflight", "--fresh")]

    # 解析參數
    if len(args) < 2:
        print_usage()
        return 1

    # 檢查模式 flag（在最後一個參數）
    mode_flags = {"--chat", "--image", "--url"}
    last_arg = args[-1]

    if last_arg in mode_flags:
        # 模式：python3 RAG.py <input> <output> --chat/--image/--url [-y]
        if len(args) != 3:
            print_usage()
            return 1
        if preflight_only:
            print("[ERROR] --preflight 只適用 PDF 文件模式（不支援 --chat/--image/--url）")
            return 1

        input_file = args[0]
        output_file = args[1]
        mode = last_arg

        if fresh and not auto_yes:
            print("[ERROR] --fresh 會清空知識庫，互動模式請加 -y 明確確認")
            return 1

        if mode == "--chat":
            if auto_yes:
                add_chat_screenshot(input_file, output_file, fresh=fresh)
            else:
                interactive_chat_screenshot(input_file, output_file)
        elif mode == "--image":
            if auto_yes:
                add_technical_image(input_file, output_file, fresh=fresh)
            else:
                interactive_technical_image(input_file, output_file)
        elif mode == "--url":
            if auto_yes:
                add_url(input_file, output_file, fresh=fresh)
            else:
                interactive_url(input_file, output_file)
        return 0

    # 一般文件模式（pdf/md/txt/bin/elf...）
    if len(args) != 2:
        print_usage()
        return 1
    input_file = args[0]
    output_file = args[1]
    try:
        add_document(input_file, output_file, preflight_only=preflight_only, fresh=fresh)
    except Exception as exc:  # noqa: BLE001 — 只攔 figure lane 的兩種，其餘原樣往上拋
        code = _pdf_cli_error_code(exc)
        if code is None:
            raise
        print(f"[ERROR] {exc}")
        return code
    return 0


if __name__ == "__main__":
    # --help / -h 短路:cheap path,不需要 llama-server / 不讀檔
    if len(sys.argv) >= 2 and sys.argv[1] in ("-h", "--help"):
        print_usage()
        sys.exit(0)

    # rebuild 子命令走自己的 argparse，不進下面的手工 parser
    if len(sys.argv) >= 2 and sys.argv[1] == "rebuild":
        sys.exit(rebuild_cli(sys.argv[2:]))

    if getattr(config_module, "KB_CONTEXT_GENERATE", False):
        print(
            "[INFO] KB_CONTEXT_GENERATE 是開的，但 chunk 脈絡只在 "
            "`python3 RAG.py rebuild --kb <kb> <doc>` 這條路徑生成；這次不生成。"
        )

    try:
        sys.exit(main(sys.argv[1:]))
    except DocumentIdentityConflict as exc:
        # 這是使用者要處理的狀況（撞名），不是程式壞掉。MCP 的 ingest_document
        # 會把這段 stdout 原樣轉給模型看，traceback 只會蓋掉真正該讀的三個選項。
        print(f"[ERROR] {exc}")
        sys.exit(1)
