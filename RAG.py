#!/usr/bin/env python3
"""
RAG 知識庫建立工具（增量模式）

用法：
    python RAG.py <input_file> <output_json>              # 一般文件（直接入庫）
    python RAG.py <screenshot> <output_json> --chat       # 聊天截圖（互動式）
    python RAG.py <image> <output_json> --image           # 技術圖片（互動式）
    python RAG.py <url> <output_json> --url               # 網頁（互動式）

範例：
    python RAG.py manual.pdf knowledge.json
    python RAG.py teams_chat.png knowledge.json --chat
    python RAG.py npx6_arch.png knowledge.json --image
    python RAG.py https://docs.example.com/guide knowledge.json --url
"""

import sys
import re
import json
import hashlib
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Tuple

# ============================================================
# 依賴檢查
# ============================================================
# 條件式依賴檢查，按模式載入
# - PDF 模式才需要 pymupdf4llm
# - VL 模式（--chat/--image）需要 llama-server VL 端點 (預設 8083)
# - --url 模式需要 html2text
# - 所有模式都需要 llama-server embedding 端點 (預設 8081)

import llama_client


def check_pymupdf4llm():
    """檢查 pymupdf4llm 套件（只有 PDF 模式需要）"""
    try:
        import pymupdf4llm
        return pymupdf4llm
    except ImportError:
        print("[ERROR] 處理 PDF 需要 pymupdf4llm 套件")
        print("請執行: pip install pymupdf4llm")
        sys.exit(1)


# ============================================================
# 設定
# ============================================================
# 改進：從 config.py 統一匯入設定，避免兩處定義不一致
try:
    from config import (
        EMBEDDING_MODEL, CHUNK_SETTINGS,
        LLAMA_EMBED_BASE_URL, LLAMA_VL_BASE_URL,
    )
except ImportError:
    EMBEDDING_MODEL = "bge-m3"  # Fallback：獨立執行時的預設值
    CHUNK_SETTINGS = {'default': {'size': 1200, 'overlap': 200}}
    LLAMA_EMBED_BASE_URL = "http://localhost:8081"
    LLAMA_VL_BASE_URL = "http://localhost:8083"

# 預設 Chunk 設定（從 CHUNK_SETTINGS 取得）
CHUNK_SIZE = CHUNK_SETTINGS.get('default', {}).get('size', 1200)
CHUNK_OVERLAP = CHUNK_SETTINGS.get('default', {}).get('overlap', 200)
INCLUDE_HEADING_IN_CONTENT = True

# Embedding 增量快取檔案
EMBEDDING_CACHE_FILE = ".rag_embedding_cache.json"

# 支援的檔案類型（文字類，process_file 走純文字抽取）
SUPPORTED_EXTENSIONS = {".pdf", ".md", ".txt"}

# 支援的圖片類型（聊天截圖模式）
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# 支援的二進位/ELF 副檔名（與 media.py 對齊；走 media.read_binary 抽報告）
BINARY_EXTENSIONS = {".bin", ".dat", ".raw", ".fw", ".img", ".rom", ".hex"}
ELF_EXTENSIONS = {".elf", ".so", ".o", ".axf", ".out", ".ko"}

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
    """
    content_upper = content.upper()
    for kw in WARNING_KEYWORDS:
        if kw.upper() in content_upper:
            return 'warning'
    return base_type


# ============================================================
# 文字處理 - 語意切分
# ============================================================
# Markdown 標題 pattern（# 開頭的行）
HEADING_PATTERN = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)

def is_heading(line: str) -> bool:
    """判斷是否為標題行"""
    line = line.strip()
    # Markdown 標題
    if re.match(r'^#{1,6}\s+', line):
        return True
    # 全大寫行（常見於 PDF 章節標題）
    if line.isupper() and len(line) > 3 and len(line) < 100:
        return True
    # 數字開頭的章節（如 "1. Introduction", "2.3 Methods"）
    if re.match(r'^\d+\.[\d\.]*\s+[A-Z]', line):
        return True
    return False

def extract_section_title(line: str) -> str:
    """從標題行提取章節名稱"""
    line = line.strip()
    # Markdown 標題
    md_match = re.match(r'^#{1,6}\s+(.+)$', line)
    if md_match:
        return md_match.group(1).strip()
    # 數字章節
    num_match = re.match(r'^(\d+\.[\d\.]*\s+.+)$', line)
    if num_match:
        return num_match.group(1).strip()
    # 全大寫標題
    if line.isupper() and len(line) > 3 and len(line) < 100:
        return line
    return ""


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
            if len(cells) >= 2 and not all(c == '-' or c.startswith('-') for c in cells):
                # 嘗試識別 key-value 對
                if len(cells) == 2:
                    normalized.append(f"{cells[0]}: {cells[1]}")
                else:
                    normalized.append(line)  # 保留原始格式
            continue

        # 條列格式 (- key: value 或 * key: value)
        list_match = re.match(r'^[\-\*\•]\s*(.+?):\s*(.+)$', line.strip())
        if list_match:
            normalized.append(f"{list_match.group(1)}: {list_match.group(2)}")
            continue

        normalized.append(line)

    return '\n'.join(normalized)


def extract_heading_hierarchy(lines: list, current_idx: int) -> str:
    """P1 改進：提取章節標題層級

    返回格式：H1 > H2 > H3
    """
    hierarchy = []

    for i in range(current_idx, -1, -1):
        line = lines[i].strip()
        if is_heading(line):
            title = extract_section_title(line)
            if title:
                # 判斷層級
                if line.startswith('### '):
                    level = 3
                elif line.startswith('## '):
                    level = 2
                elif line.startswith('# '):
                    level = 1
                elif line.isupper():
                    level = 1
                else:
                    level = 2

                # 插入到正確位置
                while hierarchy and hierarchy[0][0] >= level:
                    hierarchy.pop(0)
                hierarchy.insert(0, (level, title))

    return ' > '.join(h[1] for h in hierarchy)


def split_by_semantic_with_sections(
    text: str,
    max_chars: int = CHUNK_SIZE,
    overlap_chars: int = CHUNK_OVERLAP,
    include_heading: bool = INCLUDE_HEADING_IN_CONTENT
) -> List[Dict]:
    """
    語意切分：按標題/段落切，保持語意完整性，同時追蹤章節標題

    P1 改進：
    - 表格/條列轉成 Key: Value 格式
    - 追蹤完整的標題層級

    Returns: List[{content: str, section: str, heading_hierarchy: str}]
    """
    text = text.strip()
    if not text:
        return []

    # P1 改進：正規化表格內容
    text = normalize_table_content(text)

    if len(text) <= max_chars:
        return [{"content": text, "section": "", "heading_hierarchy": ""}]

    lines = text.split('\n')
    chunks = []
    current_chunk = []
    current_len = 0
    current_section = ""  # 追蹤當前章節
    chunk_start_idx = 0  # 用於計算 heading hierarchy

    for idx, line in enumerate(lines):
        line_len = len(line) + 1  # +1 for newline

        # 遇到標題 → 先 flush 舊 chunk（用舊 section），再更新 section
        if is_heading(line):
            # 先 flush 舊 chunk（保持舊的 section）
            if current_chunk:
                chunk_text = '\n'.join(current_chunk).strip()
                if chunk_text:
                    hierarchy = extract_heading_hierarchy(lines, chunk_start_idx)
                    chunks.append({
                        "content": chunk_text,
                        "section": current_section,
                        "heading_hierarchy": hierarchy
                    })

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
                    hierarchy = extract_heading_hierarchy(lines, chunk_start_idx)
                    chunks.append({
                        "content": chunk_text,
                        "section": current_section,
                        "heading_hierarchy": hierarchy
                    })
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
                    hierarchy = extract_heading_hierarchy(lines, chunk_start_idx)
                    chunks.append({
                        "content": chunk_text,
                        "section": current_section,
                        "heading_hierarchy": hierarchy
                    })

            # 單行超長 → 按句子切
            if line_len > max_chars:
                sub_chunks = split_long_paragraph(line, max_chars)
                for i, sc in enumerate(sub_chunks[:-1]):
                    hierarchy = extract_heading_hierarchy(lines, idx)
                    chunks.append({
                        "content": sc,
                        "section": current_section,
                        "heading_hierarchy": hierarchy
                    })
                current_chunk = [sub_chunks[-1]] if sub_chunks else []
                current_len = len(current_chunk[0]) if current_chunk else 0
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
            hierarchy = extract_heading_hierarchy(lines, chunk_start_idx)
            chunks.append({
                "content": chunk_text,
                "section": current_section,
                "heading_hierarchy": hierarchy
            })

    chunks = [c for c in chunks if c["content"].strip()]

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
                chunks[i]["content"] = tail + "\n" + chunks[i]["content"]

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
                chunk["content"] = "\n".join(header_lines) + "\n" + chunk["content"]

    return chunks


def split_by_semantic(text: str, max_chars: int = CHUNK_SIZE) -> List[str]:
    """
    語意切分：按標題/段落切，保持語意完整性
    （向後相容的簡化版本）
    """
    results = split_by_semantic_with_sections(text, max_chars)
    return [r["content"] for r in results]

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
def extract_pdf(file_path: str) -> List[Dict]:
    """提取 PDF 內容，保留頁碼、文件類型、章節"""
    # 延遲載入 pymupdf4llm（只有 PDF 模式需要）
    pymupdf4llm = check_pymupdf4llm()

    try:
        pages = pymupdf4llm.to_markdown(file_path, page_chunks=True, write_images=False)
    except Exception as e:
        print(f"  [WARN] 無法處理 PDF: {e}")
        return []

    results = []
    filename = Path(file_path).name
    doc_type = detect_doc_type(filename)
    last_section = ""  # 跨頁追蹤章節

    # 改進：若檔名無法識別類型，使用內容特徵輔助判斷
    if doc_type == 'doc' and pages:
        # 取第一頁內容作為特徵樣本
        first_page_content = pages[0].get('text', '') if pages else ''
        doc_type = detect_doc_type_by_content(first_page_content, doc_type)

    # 根據文件類型取得 chunk 設定
    chunk_size, chunk_overlap = get_chunk_settings(doc_type)

    for page_info in pages:
        page_num = page_info.get('metadata', {}).get('page', 0) + 1
        content = page_info.get('text', '').strip()

        if not content:
            continue

        # 使用帶章節的切分（根據文件類型調整 chunk 大小）
        chunk_results = split_by_semantic_with_sections(
            content, max_chars=chunk_size, overlap_chars=chunk_overlap
        )
        for i, chunk_data in enumerate(chunk_results):
            section = chunk_data["section"] or last_section
            if chunk_data["section"]:
                last_section = chunk_data["section"]

            # 根據內容判斷是否為警告類型
            chunk_type = detect_content_type(chunk_data["content"], doc_type)

            results.append({
                "source": filename,
                "page": page_num,
                "chunk_index": i,
                "content": chunk_data["content"],
                "type": chunk_type,
                "section": section,
                "heading_hierarchy": chunk_data.get("heading_hierarchy", "")
            })

    return results


def extract_text_file(file_path: str) -> List[Dict]:
    """提取純文字檔案（md, txt），包含文件類型和章節"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        print(f"  [WARN] 無法讀取檔案: {e}")
        return []

    results = []
    filename = Path(file_path).name
    doc_type = detect_doc_type(filename)

    # 改進：若檔名無法識別類型，使用內容特徵輔助判斷
    if doc_type == 'doc':
        doc_type = detect_doc_type_by_content(content, doc_type)

    # 根據文件類型取得 chunk 設定
    chunk_size, chunk_overlap = get_chunk_settings(doc_type)

    # 使用帶章節的切分（根據文件類型調整 chunk 大小）
    chunk_results = split_by_semantic_with_sections(
        content, max_chars=chunk_size, overlap_chars=chunk_overlap
    )
    for i, chunk_data in enumerate(chunk_results):
        # 根據內容判斷是否為警告類型
        chunk_type = detect_content_type(chunk_data["content"], doc_type)

        results.append({
            "source": filename,
            "page": 1,  # 純文字檔案視為單頁
            "chunk_index": i,
            "content": chunk_data["content"],
            "type": chunk_type,
            "section": chunk_data["section"],
            "heading_hierarchy": chunk_data.get("heading_hierarchy", "")
        })

    return results

def extract_binary(file_path: str) -> List[Dict]:
    """提取 binary/ELF 內容（hex dump、magic、可讀字串、ELF symbol 等）。

    用 media.read_binary 抽出可分析的 Markdown 報告（遇到 ELF magic 會自動切到
    ELF 解析）。報告裡 【...】 章節標記會被轉成 ## 標題，方便語意切分。
    """
    # 延遲載入 media（其他模式不需要）
    try:
        from media import read_binary, set_sandbox_root
    except ImportError as e:
        print(f"[ERROR] binary/ELF 模式需要 media 模組: {e}")
        return []

    p = Path(file_path).resolve()
    if not p.is_file():
        print(f"  [WARN] 檔案不存在: {file_path}")
        return []

    # read_binary 內建沙箱（_SANDBOX_ROOT），這裡設成檔案所在目錄即可
    set_sandbox_root(str(p.parent), allow_external=False)

    content = read_binary(str(p))
    if (not content
            or content.startswith("[BIN 錯誤]")
            or content.startswith("[ELF 錯誤]")):
        print(f"  [WARN] binary 分析失敗: {content[:200] if content else '空結果'}")
        return []

    # 把 【標題】(尾巴) 轉成 ## 標題 尾巴，讓 split_by_semantic_with_sections 抓得到章節
    # （media.py 的報告會出現「【可讀字串（含 offset）】共 3 個」這種尾巴帶文字的行）
    content = re.sub(r'^【(.+?)】(.*)$', r'## \1\2', content, flags=re.MULTILINE)

    filename = p.name
    chunk_results = split_by_semantic_with_sections(content)

    results: List[Dict] = []
    last_section = ""
    for i, chunk_data in enumerate(chunk_results):
        section = chunk_data["section"] or last_section
        if chunk_data["section"]:
            last_section = chunk_data["section"]

        chunk_type = detect_content_type(chunk_data["content"], "binary")

        results.append({
            "source": filename,
            "page": 1,
            "chunk_index": i,
            "content": chunk_data["content"],
            "type": chunk_type,
            "section": section,
            "heading_hierarchy": chunk_data.get("heading_hierarchy", ""),
            "origin": "binary",
        })

    return results


def process_file(file_path: str) -> List[Dict]:
    """根據檔案類型選擇處理方式"""
    ext = Path(file_path).suffix.lower()

    if ext == ".pdf":
        return extract_pdf(file_path)
    elif ext in {".md", ".txt"}:
        return extract_text_file(file_path)
    elif ext in BINARY_EXTENSIONS or ext in ELF_EXTENSIONS:
        return extract_binary(file_path)
    else:
        return []

# ============================================================
# 聊天截圖處理
# ============================================================
def extract_chat_from_screenshot(image_path: str) -> str:
    """
    使用 VL 模型從截圖中提取聊天內容並整理成結構化摘要
    """
    import base64

    # 載入 VL 模型設定
    try:
        from config import VL_MODEL
    except ImportError:
        VL_MODEL = "llava"  # Fallback

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
        data = llama_client.native_completion(
            base_url=LLAMA_VL_BASE_URL,
            prompt=prompt,
            temperature=0.2,
            stream=False,
            image_data=[{"id": 10, "data": image_data}],
            timeout=300,
        )
        return data.get("content") or data.get("response") or ""
    except Exception as e:
        print(f"[ERROR] VL 模型處理失敗: {e}")
        return ""


def process_chat_screenshot(image_path: str) -> List[Dict]:
    """處理聊天截圖，提取並整理成知識區塊"""
    print(f"[INFO] 使用 VL 模型分析截圖...")

    # 提取聊天內容
    content = extract_chat_from_screenshot(image_path)

    if not content:
        return []

    print(f"[INFO] 提取完成，內容長度: {len(content)} 字元")
    print("-" * 40)
    print(content[:500] + "..." if len(content) > 500 else content)
    print("-" * 40)

    # 切分成 chunks
    results = []
    filename = Path(image_path).name

    chunk_results = split_by_semantic_with_sections(content)
    for i, chunk_data in enumerate(chunk_results):
        chunk_type = detect_content_type(chunk_data["content"], 'chat')

        results.append({
            "source": f"chat_{filename}",
            "page": 1,
            "chunk_index": i,
            "content": chunk_data["content"],
            "type": chunk_type,
            "section": chunk_data["section"],
            "heading_hierarchy": chunk_data.get("heading_hierarchy", ""),
            "origin": "screenshot"  # 標記來源是截圖
        })

    return results


# ============================================================
# 技術圖片處理
# ============================================================
def extract_info_from_image(image_path: str) -> str:
    """
    使用 VL 模型從技術圖片中提取資訊並整理成結構化文件
    適用於：架構圖、流程圖、記憶體映射圖、硬體方塊圖等
    """
    import base64

    # 載入 VL 模型設定
    try:
        from config import VL_MODEL
    except ImportError:
        VL_MODEL = "llava"  # Fallback

    # 讀取圖片並轉 base64
    with open(image_path, 'rb') as f:
        image_data = base64.b64encode(f.read()).decode('utf-8')

    # 提示詞：針對技術圖片的分析
    # 增加「原始文字摘錄」層，降低幻覺風險
    prompt = """請詳細分析這張技術圖片，並整理成結構化的技術文件。

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

    try:
        data = llama_client.native_completion(
            base_url=LLAMA_VL_BASE_URL,
            prompt=prompt,
            temperature=0.2,
            stream=False,
            image_data=[{"id": 10, "data": image_data}],
            timeout=300,
        )
        return data.get("content") or data.get("response") or ""
    except Exception as e:
        print(f"[ERROR] VL 模型處理失敗: {e}")
        return ""


def process_technical_image(image_path: str) -> List[Dict]:
    """處理技術圖片，提取並整理成知識區塊"""
    print(f"[INFO] 使用 VL 模型分析技術圖片...")

    # 提取圖片資訊
    content = extract_info_from_image(image_path)

    if not content:
        return []

    print(f"[INFO] 提取完成，內容長度: {len(content)} 字元")
    print("-" * 40)
    print(content[:500] + "..." if len(content) > 500 else content)
    print("-" * 40)

    # 切分成 chunks
    results = []
    filename = Path(image_path).name

    chunk_results = split_by_semantic_with_sections(content)
    for i, chunk_data in enumerate(chunk_results):
        chunk_type = detect_content_type(chunk_data["content"], 'diagram')

        results.append({
            "source": f"image_{filename}",
            "page": 1,
            "chunk_index": i,
            "content": chunk_data["content"],
            "type": chunk_type,
            "section": chunk_data["section"],
            "heading_hierarchy": chunk_data.get("heading_hierarchy", ""),
            "origin": "image"  # 標記來源是技術圖片
        })

    return results


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


def generate_embeddings(chunks: List[Dict], cache_dir: Path = None) -> List[Dict]:
    """為所有 chunks 生成 embeddings

    改進：使用內容雜湊快取，避免重複計算相同內容的 embedding
    - 快取 key = content 的 MD5 雜湊
    - 相同內容直接從快取取得 embedding
    - 新內容計算後存入快取
    """
    total = len(chunks)

    # 載入快取
    if cache_dir is None:
        cache_dir = Path.cwd()
    cache_path = cache_dir / EMBEDDING_CACHE_FILE
    cache = _load_embedding_cache(cache_path)
    cache_hits = 0
    cache_updated = False

    for i, chunk in enumerate(chunks):
        # 進度顯示
        if (i + 1) % 10 == 0 or i == 0 or i == total - 1:
            status = f"(快取命中: {cache_hits})" if cache_hits > 0 else ""
            print(f"  Embedding: {i + 1}/{total} {status}", end='\r')

        content = chunk['content']
        content_key = _content_hash(content)

        # 檢查快取
        if content_key in cache:
            chunk['embedding'] = cache[content_key]
            cache_hits += 1
            continue

        # 計算新的 embedding(走 llama-server /embedding)
        try:
            embedding = llama_client.embed_one(
                base_url=LLAMA_EMBED_BASE_URL,
                content=content,
                model=EMBEDDING_MODEL,
                timeout=120,
            )
            chunk['embedding'] = embedding
            # 存入快取
            cache[content_key] = embedding
            cache_updated = True
        except Exception as e:
            print(f"\n  [ERROR] Embedding 失敗: {e}")
            chunk['embedding'] = []

    # 儲存更新後的快取
    if cache_updated:
        _save_embedding_cache(cache_path, cache)
        print(f"\n  [INFO] Embedding 快取已更新 ({len(cache)} 項)")
    elif cache_hits > 0:
        print(f"\n  [INFO] 快取命中 {cache_hits}/{total} ({cache_hits*100//total}%)")
    else:
        print()  # 換行

    return chunks

# ============================================================
# 主程式
# ============================================================
def load_knowledge_base(output_path: Path) -> Dict:
    """載入現有知識庫，不存在則建立空的"""
    if output_path.exists():
        try:
            with open(output_path, 'r', encoding='utf-8') as f:
                kb = json.load(f)
            _restore_embeddings_from_npz(kb, output_path)
            print(f"[INFO] 載入現有知識庫: {len(kb.get('chunks', []))} 個區塊")
            return kb
        except Exception as e:
            print(f"[WARN] 無法讀取現有知識庫，將建立新的: {e}")
    
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

def _chunks_content_hash(chunks: List[Dict]) -> str:
    """計算 chunks 內容 hash，需與 save_knowledge_base 的 .npz metadata 一致。"""
    hasher = hashlib.md5()
    for chunk in chunks:
        hasher.update(chunk.get('content', '').encode('utf-8'))
    return hasher.hexdigest()


def _restore_embeddings_from_npz(kb: Dict, output_path: Path) -> bool:
    """把 JSON 外掛的 .npz embeddings 補回 chunks。

    RAG JSON 為了減少體積不再保存 embedding；增量新增文件前必須先把舊
    embeddings 還原，否則下一次 save 會把舊 chunks 寫成零向量。
    """
    chunks = kb.get("chunks", [])
    if not chunks:
        return False
    if all(chunk.get("embedding") for chunk in chunks):
        return True

    try:
        import numpy as np
        from config import KNOWLEDGE_EMB_FILE
    except ImportError:
        return False

    emb_path = output_path.parent / KNOWLEDGE_EMB_FILE
    if not emb_path.exists():
        return False

    try:
        data = np.load(emb_path, allow_pickle=True)
        embeddings = data["embeddings"]
        chunk_count = int(data.get("chunk_count", 0))
        content_hash = str(data.get("content_hash", ""))
    except Exception as e:
        print(f"[WARN] 載入既有 .npz embeddings 失敗，將在儲存時重算缺失向量: {e}")
        return False

    if chunk_count != len(chunks):
        print("[WARN] 既有 .npz chunk 數量不一致，將在儲存時重算缺失向量")
        return False

    current_hash = _chunks_content_hash(chunks)
    if content_hash and content_hash != current_hash:
        print("[WARN] 既有 .npz 內容 hash 不一致，將在儲存時重算缺失向量")
        return False

    for chunk, emb in zip(chunks, embeddings):
        if not chunk.get("embedding"):
            chunk["embedding"] = emb.tolist() if hasattr(emb, "tolist") else list(emb)
    return True


def save_knowledge_base(kb: Dict, output_path: Path):
    """儲存知識庫

    改進：將 embeddings 完全移到 .npz，JSON 只存文字與 metadata
    - 大幅減少 JSON 檔案大小
    - 加速 JSON 解析
    - .npz 使用壓縮格式，整體儲存更有效率
    """
    missing_embeddings = [chunk for chunk in kb.get("chunks", []) if not chunk.get("embedding")]
    if missing_embeddings:
        print(f"[WARN] {len(missing_embeddings)} 個舊 chunks 缺少 embedding，將重算以避免寫入零向量")
        generate_embeddings(missing_embeddings)

    # 更新 metadata
    kb["metadata"]["updated_at"] = datetime.now().isoformat()
    kb["metadata"]["total_documents"] = len(kb["metadata"]["documents"])
    kb["metadata"]["total_chunks"] = len(kb["chunks"])

    # 分離 embeddings（從 chunks 移除，存到 .npz）
    embeddings = []
    chunks_without_emb = []
    for chunk in kb["chunks"]:
        emb = chunk.pop("embedding", [])
        embeddings.append(emb)
        chunks_without_emb.append(chunk)

    # 儲存 JSON（不含 embedding）
    kb_to_save = {
        "metadata": kb["metadata"],
        "chunks": chunks_without_emb
    }
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(kb_to_save, f, ensure_ascii=False, indent=2)

    # 儲存 embeddings 到 .npz
    try:
        import numpy as np
        from config import KNOWLEDGE_EMB_FILE
        emb_path = output_path.parent / KNOWLEDGE_EMB_FILE

        # 計算 content hash（用於載入時驗證）
        content_hash = _chunks_content_hash(chunks_without_emb)

        # 確保 embeddings 維度一致
        non_empty_embeddings = [e for e in embeddings if e]
        if non_empty_embeddings:
            emb_dim = max(len(e) for e in non_empty_embeddings)
            normalized = []
            for emb in embeddings:
                if len(emb) == emb_dim:
                    normalized.append(emb)
                elif len(emb) == 0:
                    normalized.append([0.0] * emb_dim)
                else:
                    normalized.append((emb + [0.0] * emb_dim)[:emb_dim])

            emb_array = np.array(normalized, dtype=np.float32)
            # L2 正規化
            norms = np.linalg.norm(emb_array, axis=1, keepdims=True)
            norms = np.where(norms > 0, norms, 1.0)
            emb_array = emb_array / norms

            np.savez_compressed(
                emb_path,
                embeddings=emb_array,
                embedding_model=EMBEDDING_MODEL,
                chunk_count=len(chunks_without_emb),
                content_hash=content_hash
            )
            emb_size = emb_path.stat().st_size / 1024 / 1024
            print(f"     Embeddings: {emb_path.name} ({emb_size:.2f} MB)")
        elif emb_path.exists():
            emb_path.unlink()
            print(f"     Embeddings: 已移除空知識庫的 {emb_path.name}")
    except ImportError:
        # numpy 不可用，將 embedding 放回 JSON（向後相容）
        for i, emb in enumerate(embeddings):
            if i < len(kb["chunks"]):
                kb["chunks"][i]["embedding"] = emb
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(kb, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"     [WARN] 儲存 .npz 失敗: {e}")

    file_size = output_path.stat().st_size / 1024 / 1024  # MB
    print(f"\n[OK] 知識庫已更新!")
    print(f"     檔案: {output_path.absolute()}")
    print(f"     大小: {file_size:.2f} MB")
    print(f"     文件數: {kb['metadata']['total_documents']}")
    print(f"     區塊數: {kb['metadata']['total_chunks']}")

def add_document(input_file: str, output_file: str):
    """將文件加入知識庫"""
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

    # 載入現有知識庫
    kb = load_knowledge_base(output_path)

    # 檢查是否已存在同名文件（若有則先移除舊的）
    doc_name = input_path.name
    if doc_name in kb["metadata"]["documents"]:
        print(f"[INFO] 更新現有文件: {doc_name}")
        # 移除舊的 chunks
        kb["chunks"] = [c for c in kb["chunks"] if c["source"] != doc_name]
        kb["metadata"]["documents"].remove(doc_name)
    else:
        print(f"[INFO] 新增文件: {doc_name}")
    
    # 處理新文件
    print(f"[INFO] 處理: {input_path.name}")
    new_chunks = process_file(str(input_path))
    
    if not new_chunks:
        print("[WARN] 沒有提取到任何內容")
        sys.exit(1)
    
    print(f"[INFO] 提取 {len(new_chunks)} 個文字區塊")
    
    # 生成 embeddings
    print(f"[INFO] 使用 {EMBEDDING_MODEL} 生成 embeddings...")
    new_chunks = generate_embeddings(new_chunks)
    
    # 為每個 chunk 生成唯一 ID
    for chunk in new_chunks:
        content_hash = hashlib.md5(chunk['content'].encode()).hexdigest()[:8]
        chunk['id'] = f"{chunk['source']}::p{chunk['page']}::c{chunk['chunk_index']}::{content_hash}"
    
    # Append 到知識庫
    kb["chunks"].extend(new_chunks)
    kb["metadata"]["documents"].append(doc_name)
    
    # 儲存
    save_knowledge_base(kb, output_path)

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


def _add_chat_content_to_kb(image_path: Path, content: str, output_file: str):
    """將已分析的聊天內容加入知識庫（內部函式）"""
    output_path = Path(output_file)

    # 自動快取分析結果
    cache_file = _save_to_cache(image_path.name, content, "chat")
    if cache_file:
        print(f"[INFO] 快取已存: {cache_file}")

    # 切分成 chunks
    chunk_results = split_by_semantic_with_sections(content)
    new_chunks = []
    for i, chunk_data in enumerate(chunk_results):
        chunk_type = detect_content_type(chunk_data["content"], 'chat')
        new_chunks.append({
            "source": f"chat_{image_path.name}",
            "page": 1,
            "chunk_index": i,
            "content": chunk_data["content"],
            "type": chunk_type,
            "section": chunk_data["section"],
            "heading_hierarchy": chunk_data.get("heading_hierarchy", ""),
            "origin": "screenshot"
        })

    if not new_chunks:
        print("[WARN] 沒有提取到任何內容")
        return

    # 載入現有知識庫
    kb = load_knowledge_base(output_path)
    doc_name = f"chat_{image_path.name}"

    # 檢查是否已存在同名文件
    if doc_name in kb["metadata"]["documents"]:
        print(f"[INFO] 更新現有截圖知識: {doc_name}")
        kb["chunks"] = [c for c in kb["chunks"] if c["source"] != doc_name]
        kb["metadata"]["documents"].remove(doc_name)
    else:
        print(f"[INFO] 新增截圖知識: {doc_name}")

    print(f"[INFO] 提取 {len(new_chunks)} 個文字區塊")

    # 生成 embeddings
    print(f"[INFO] 使用 {EMBEDDING_MODEL} 生成 embeddings...")
    new_chunks = generate_embeddings(new_chunks)

    # 為每個 chunk 生成唯一 ID
    for chunk in new_chunks:
        content_hash = hashlib.md5(chunk['content'].encode()).hexdigest()[:8]
        chunk['id'] = f"{chunk['source']}::p{chunk['page']}::c{chunk['chunk_index']}::{content_hash}"

    # Append 到知識庫
    kb["chunks"].extend(new_chunks)
    kb["metadata"]["documents"].append(doc_name)

    # 儲存
    save_knowledge_base(kb, output_path)


def add_chat_screenshot(image_file: str, output_file: str):
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

    # 載入現有知識庫
    kb = load_knowledge_base(output_path)

    # 檢查是否已存在同名文件（若有則先移除舊的）
    doc_name = f"chat_{image_path.name}"
    if doc_name in kb["metadata"]["documents"]:
        print(f"[INFO] 更新現有截圖知識: {doc_name}")
        kb["chunks"] = [c for c in kb["chunks"] if c["source"] != doc_name]
        kb["metadata"]["documents"].remove(doc_name)
    else:
        print(f"[INFO] 新增截圖知識: {doc_name}")

    # 處理截圖
    print(f"[INFO] 處理: {image_path.name}")
    new_chunks = process_chat_screenshot(str(image_path))

    if not new_chunks:
        print("[WARN] 沒有提取到任何內容")
        sys.exit(1)

    print(f"[INFO] 提取 {len(new_chunks)} 個文字區塊")

    # 生成 embeddings
    print(f"[INFO] 使用 {EMBEDDING_MODEL} 生成 embeddings...")
    new_chunks = generate_embeddings(new_chunks)

    # 為每個 chunk 生成唯一 ID
    for chunk in new_chunks:
        content_hash = hashlib.md5(chunk['content'].encode()).hexdigest()[:8]
        chunk['id'] = f"{chunk['source']}::p{chunk['page']}::c{chunk['chunk_index']}::{content_hash}"

    # Append 到知識庫
    kb["chunks"].extend(new_chunks)
    kb["metadata"]["documents"].append(doc_name)

    # 儲存
    save_knowledge_base(kb, output_path)


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


def process_url(url: str) -> Optional[Tuple[List[Dict], str]]:
    """處理網頁 URL，提取內容並整理成知識區塊

    Returns:
        成功: (chunks, url_name) tuple
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

    # 使用更穩定的命名避免撞名
    url_name = generate_url_name(url)

    # 切分成 chunks
    results = []
    chunk_results = split_by_semantic_with_sections(content)

    for i, chunk_data in enumerate(chunk_results):
        chunk_type = detect_content_type(chunk_data["content"], 'web')

        results.append({
            "source": f"url_{url_name}",
            "page": 1,
            "chunk_index": i,
            "content": chunk_data["content"],
            "type": chunk_type,
            "section": chunk_data["section"],
            "heading_hierarchy": chunk_data.get("heading_hierarchy", ""),
            "origin": "url",
            "url": url,              # 保留原始 URL
            "title": title,          # 補存標題
            "fetched_at": fetched_at # 補存抓取時間
        })

    return results, url_name  # 回傳 url_name 供 add_url 使用


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
    output_path = Path(output_file)
    url_name = generate_url_name(url)
    fetched_at = datetime.now().isoformat()

    # 自動快取抓取結果
    cache_file = _save_to_cache(url, content, "url", {"title": title})
    if cache_file:
        print(f"[INFO] 快取已存: {cache_file}")

    # 切分成 chunks
    chunk_results = split_by_semantic_with_sections(content)
    new_chunks = []
    for i, chunk_data in enumerate(chunk_results):
        chunk_type = detect_content_type(chunk_data["content"], 'web')
        new_chunks.append({
            "source": f"url_{url_name}",
            "page": 1,
            "chunk_index": i,
            "content": chunk_data["content"],
            "type": chunk_type,
            "section": chunk_data["section"],
            "heading_hierarchy": chunk_data.get("heading_hierarchy", ""),
            "origin": "url",
            "url": url,
            "title": title,
            "fetched_at": fetched_at
        })

    if not new_chunks:
        print("[WARN] 沒有提取到任何內容")
        return

    # 載入現有知識庫
    kb = load_knowledge_base(output_path)
    doc_name = f"url_{url_name}"

    # 檢查是否已存在同名文件
    if doc_name in kb["metadata"]["documents"]:
        print(f"[INFO] 更新現有網頁知識: {doc_name}")
        kb["chunks"] = [c for c in kb["chunks"] if c["source"] != doc_name]
        kb["metadata"]["documents"].remove(doc_name)
    else:
        print(f"[INFO] 新增網頁知識: {doc_name}")

    print(f"[INFO] 提取 {len(new_chunks)} 個文字區塊")

    # 生成 embeddings
    print(f"[INFO] 使用 {EMBEDDING_MODEL} 生成 embeddings...")
    new_chunks = generate_embeddings(new_chunks)

    # 為每個 chunk 生成唯一 ID
    for chunk in new_chunks:
        content_hash = hashlib.md5(chunk['content'].encode()).hexdigest()[:8]
        chunk['id'] = f"{chunk['source']}::p{chunk['page']}::c{chunk['chunk_index']}::{content_hash}"

    # Append 到知識庫
    kb["chunks"].extend(new_chunks)
    kb["metadata"]["documents"].append(doc_name)

    # 儲存
    save_knowledge_base(kb, output_path)


def add_url(url: str, output_file: str):
    """將網頁內容加入知識庫（相容舊 API，直接入庫不詢問）"""
    output_path = Path(output_file)

    # 簡單驗證 URL 格式
    if not url.startswith(('http://', 'https://')):
        print(f"[ERROR] 無效的 URL: {url}")
        print("        URL 必須以 http:// 或 https:// 開頭")
        sys.exit(1)

    # 載入現有知識庫
    kb = load_knowledge_base(output_path)

    # 處理網頁（會回傳 (chunks, url_name) 或 None）
    result = process_url(url)

    if result is None:
        print("[ERROR] 無法從網頁提取內容，新增失敗")
        sys.exit(1)

    new_chunks, url_name = result
    doc_name = f"url_{url_name}"

    # 檢查是否已存在同名文件
    if doc_name in kb["metadata"]["documents"]:
        print(f"[INFO] 更新現有網頁知識: {doc_name}")
        kb["chunks"] = [c for c in kb["chunks"] if c["source"] != doc_name]
        kb["metadata"]["documents"].remove(doc_name)
    else:
        print(f"[INFO] 新增網頁知識: {doc_name}")

    print(f"[INFO] 提取 {len(new_chunks)} 個文字區塊")

    # 生成 embeddings
    print(f"[INFO] 使用 {EMBEDDING_MODEL} 生成 embeddings...")
    new_chunks = generate_embeddings(new_chunks)

    # 為每個 chunk 生成唯一 ID
    for chunk in new_chunks:
        content_hash = hashlib.md5(chunk['content'].encode()).hexdigest()[:8]
        chunk['id'] = f"{chunk['source']}::p{chunk['page']}::c{chunk['chunk_index']}::{content_hash}"

    # Append 到知識庫
    kb["chunks"].extend(new_chunks)
    kb["metadata"]["documents"].append(doc_name)

    # 儲存
    save_knowledge_base(kb, output_path)


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
    output_path = Path(output_file)

    # 自動快取分析結果
    cache_file = _save_to_cache(image_path.name, content, "image")
    if cache_file:
        print(f"[INFO] 快取已存: {cache_file}")

    # 切分成 chunks
    chunk_results = split_by_semantic_with_sections(content)
    new_chunks = []
    for i, chunk_data in enumerate(chunk_results):
        chunk_type = detect_content_type(chunk_data["content"], 'diagram')
        new_chunks.append({
            "source": f"image_{image_path.name}",
            "page": 1,
            "chunk_index": i,
            "content": chunk_data["content"],
            "type": chunk_type,
            "section": chunk_data["section"],
            "heading_hierarchy": chunk_data.get("heading_hierarchy", ""),
            "origin": "image"
        })

    if not new_chunks:
        print("[WARN] 沒有提取到任何內容")
        return

    # 載入現有知識庫
    kb = load_knowledge_base(output_path)
    doc_name = f"image_{image_path.name}"

    # 檢查是否已存在同名文件
    if doc_name in kb["metadata"]["documents"]:
        print(f"[INFO] 更新現有圖片知識: {doc_name}")
        kb["chunks"] = [c for c in kb["chunks"] if c["source"] != doc_name]
        kb["metadata"]["documents"].remove(doc_name)
    else:
        print(f"[INFO] 新增圖片知識: {doc_name}")

    print(f"[INFO] 提取 {len(new_chunks)} 個文字區塊")

    # 生成 embeddings
    print(f"[INFO] 使用 {EMBEDDING_MODEL} 生成 embeddings...")
    new_chunks = generate_embeddings(new_chunks)

    # 為每個 chunk 生成唯一 ID
    for chunk in new_chunks:
        content_hash = hashlib.md5(chunk['content'].encode()).hexdigest()[:8]
        chunk['id'] = f"{chunk['source']}::p{chunk['page']}::c{chunk['chunk_index']}::{content_hash}"

    # Append 到知識庫
    kb["chunks"].extend(new_chunks)
    kb["metadata"]["documents"].append(doc_name)

    # 儲存
    save_knowledge_base(kb, output_path)


def add_technical_image(image_file: str, output_file: str):
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

    # 載入現有知識庫
    kb = load_knowledge_base(output_path)

    # 檢查是否已存在同名文件（若有則先移除舊的）
    doc_name = f"image_{image_path.name}"
    if doc_name in kb["metadata"]["documents"]:
        print(f"[INFO] 更新現有圖片知識: {doc_name}")
        kb["chunks"] = [c for c in kb["chunks"] if c["source"] != doc_name]
        kb["metadata"]["documents"].remove(doc_name)
    else:
        print(f"[INFO] 新增圖片知識: {doc_name}")

    # 處理圖片
    print(f"[INFO] 處理: {image_path.name}")
    new_chunks = process_technical_image(str(image_path))

    if not new_chunks:
        print("[WARN] 沒有提取到任何內容")
        sys.exit(1)

    print(f"[INFO] 提取 {len(new_chunks)} 個文字區塊")

    # 生成 embeddings
    print(f"[INFO] 使用 {EMBEDDING_MODEL} 生成 embeddings...")
    new_chunks = generate_embeddings(new_chunks)

    # 為每個 chunk 生成唯一 ID
    for chunk in new_chunks:
        content_hash = hashlib.md5(chunk['content'].encode()).hexdigest()[:8]
        chunk['id'] = f"{chunk['source']}::p{chunk['page']}::c{chunk['chunk_index']}::{content_hash}"

    # Append 到知識庫
    kb["chunks"].extend(new_chunks)
    kb["metadata"]["documents"].append(doc_name)

    # 儲存
    save_knowledge_base(kb, output_path)


# ============================================================
# 入口
# ============================================================
def print_usage():
    """印出使用說明"""
    print("用法:")
    print("  python RAG.py <input_file> <output_json>             # 一般文件（直接入庫）")
    print("  python RAG.py <screenshot> <output_json> --chat      # 聊天截圖（互動式）")
    print("  python RAG.py <image> <output_json> --image          # 技術圖片（互動式）")
    print("  python RAG.py <url> <output_json> --url              # 網頁（互動式）")
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
    print("  output_json  知識庫檔案 (不存在則建立，存在則 append)")
    print("")
    print("範例:")
    print("  python RAG.py manual.pdf knowledge.json                       # PDF 直接入庫")
    print("  python RAG.py firmware.bin knowledge.json                     # binary/ELF 直接入庫")
    print("  python RAG.py teams_chat.png knowledge.json --chat            # 聊天截圖")
    print("  python RAG.py npx6_arch.png knowledge.json --image            # 技術圖片")
    print("  python RAG.py teams_chat.png knowledge.json --chat -y         # 同上但不問")
    print("  python RAG.py https://docs.example.com/guide knowledge.json --url  # 網頁")
    print("")
    print(f"支援的文字類型: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
    print(f"支援的圖片類型: {', '.join(sorted(IMAGE_EXTENSIONS))}")
    print(f"支援的二進位類型: {', '.join(sorted(BINARY_EXTENSIONS))}")
    print(f"支援的 ELF 類型: {', '.join(sorted(ELF_EXTENSIONS))}")


if __name__ == "__main__":
    # --help / -h 短路:cheap path,不需要 llama-server / 不讀檔
    if len(sys.argv) >= 2 and sys.argv[1] in ("-h", "--help"):
        print_usage()
        sys.exit(0)

    # 抽出 -y / --yes 旗標（位置不限），剩下的當作正常參數
    args = [a for a in sys.argv[1:] if a not in ("-y", "--yes")]
    auto_yes = len(args) != len(sys.argv) - 1

    # 解析參數
    if len(args) < 2:
        print_usage()
        sys.exit(1)

    # 檢查模式 flag（在最後一個參數）
    mode_flags = {"--chat", "--image", "--url"}
    last_arg = args[-1]

    if last_arg in mode_flags:
        # 模式：python RAG.py <input> <output> --chat/--image/--url [-y]
        if len(args) != 3:
            print_usage()
            sys.exit(1)

        input_file = args[0]
        output_file = args[1]
        mode = last_arg

        if mode == "--chat":
            if auto_yes:
                add_chat_screenshot(input_file, output_file)
            else:
                interactive_chat_screenshot(input_file, output_file)
        elif mode == "--image":
            if auto_yes:
                add_technical_image(input_file, output_file)
            else:
                interactive_technical_image(input_file, output_file)
        elif mode == "--url":
            if auto_yes:
                add_url(input_file, output_file)
            else:
                interactive_url(input_file, output_file)

    # 一般文件模式（pdf/md/txt/bin/elf...）
    else:
        if len(args) != 2:
            print_usage()
            sys.exit(1)
        input_file = args[0]
        output_file = args[1]
        add_document(input_file, output_file)
