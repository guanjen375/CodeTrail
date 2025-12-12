#!/usr/bin/env python3
"""
RAG 知識庫建立工具（增量模式）
用法：python RAG.py <input_file> <output_json>

範例：
    python RAG.py docs/manual.pdf knowledge.json
    python RAG.py docs/ knowledge.json  # 處理整個目錄
"""

import sys
import re
import json
import hashlib
from pathlib import Path
from datetime import datetime
from typing import List, Dict

# ============================================================
# 依賴檢查
# ============================================================
def check_dependencies():
    """檢查必要套件"""
    missing = []
    
    try:
        import pymupdf4llm
    except ImportError:
        missing.append("pymupdf4llm")
    
    try:
        import ollama
    except ImportError:
        missing.append("ollama")
    
    if missing:
        print(f"[ERROR] 缺少套件: {', '.join(missing)}")
        print(f"請執行: pip install {' '.join(missing)}")
        sys.exit(1)

check_dependencies()

import pymupdf4llm
import ollama

# ============================================================
# 設定
# ============================================================
# 改進：從 config.py 統一匯入 EMBEDDING_MODEL，避免兩處定義不一致
# 這樣換 embedding model 時只需改 config.py 一處
try:
    from config import EMBEDDING_MODEL
except ImportError:
    EMBEDDING_MODEL = "bge-m3"  # Fallback：獨立執行時的預設值

CHUNK_SIZE = 1200           # 每個 chunk 的最大字元數（增加以保留完整指令）

# 支援的檔案類型
SUPPORTED_EXTENSIONS = {".pdf", ".md", ".txt"}

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


def split_by_semantic_with_sections(text: str, max_chars: int = CHUNK_SIZE) -> List[Dict]:
    """
    語意切分：按標題/段落切，保持語意完整性，同時追蹤章節標題

    Returns: List[{content: str, section: str}]
    """
    text = text.strip()
    if not text:
        return []

    if len(text) <= max_chars:
        return [{"content": text, "section": ""}]

    lines = text.split('\n')
    chunks = []
    current_chunk = []
    current_len = 0
    current_section = ""  # 追蹤當前章節

    for line in lines:
        line_len = len(line) + 1  # +1 for newline

        # 遇到標題 → 先 flush 舊 chunk（用舊 section），再更新 section
        if is_heading(line):
            # 先 flush 舊 chunk（保持舊的 section）
            if current_chunk:
                chunk_text = '\n'.join(current_chunk).strip()
                if chunk_text:
                    chunks.append({"content": chunk_text, "section": current_section})

            # 再更新 section
            section_title = extract_section_title(line)
            if section_title:
                current_section = section_title

            current_chunk = [line]
            current_len = line_len
            continue

        # 空行 → 段落分界
        if not line.strip():
            if current_len > max_chars * 0.7:  # 超過 70% 就切
                chunk_text = '\n'.join(current_chunk).strip()
                if chunk_text:
                    chunks.append({"content": chunk_text, "section": current_section})
                current_chunk = []
                current_len = 0
            else:
                current_chunk.append(line)
                current_len += line_len
            continue

        # 加入當前行會超過限制 → 切分
        if current_len + line_len > max_chars:
            if current_chunk:
                chunk_text = '\n'.join(current_chunk).strip()
                if chunk_text:
                    chunks.append({"content": chunk_text, "section": current_section})

            # 單行超長 → 按句子切
            if line_len > max_chars:
                sub_chunks = split_long_paragraph(line, max_chars)
                for i, sc in enumerate(sub_chunks[:-1]):
                    chunks.append({"content": sc, "section": current_section})
                current_chunk = [sub_chunks[-1]] if sub_chunks else []
                current_len = len(current_chunk[0]) if current_chunk else 0
            else:
                current_chunk = [line]
                current_len = line_len
        else:
            current_chunk.append(line)
            current_len += line_len

    # 處理最後的 chunk
    if current_chunk:
        chunk_text = '\n'.join(current_chunk).strip()
        if chunk_text:
            chunks.append({"content": chunk_text, "section": current_section})

    return [c for c in chunks if c["content"].strip()]


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
    try:
        pages = pymupdf4llm.to_markdown(file_path, page_chunks=True, write_images=False)
    except Exception as e:
        print(f"  [WARN] 無法處理 PDF: {e}")
        return []

    results = []
    filename = Path(file_path).name
    doc_type = detect_doc_type(filename)
    last_section = ""  # 跨頁追蹤章節

    for page_info in pages:
        page_num = page_info.get('metadata', {}).get('page', 0) + 1
        content = page_info.get('text', '').strip()

        if not content:
            continue

        # 使用帶章節的切分
        chunk_results = split_by_semantic_with_sections(content)
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
                "section": section
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

    # 使用帶章節的切分
    chunk_results = split_by_semantic_with_sections(content)
    for i, chunk_data in enumerate(chunk_results):
        # 根據內容判斷是否為警告類型
        chunk_type = detect_content_type(chunk_data["content"], doc_type)

        results.append({
            "source": filename,
            "page": 1,  # 純文字檔案視為單頁
            "chunk_index": i,
            "content": chunk_data["content"],
            "type": chunk_type,
            "section": chunk_data["section"]
        })

    return results

def process_file(file_path: str) -> List[Dict]:
    """根據檔案類型選擇處理方式"""
    ext = Path(file_path).suffix.lower()
    
    if ext == ".pdf":
        return extract_pdf(file_path)
    elif ext in {".md", ".txt"}:
        return extract_text_file(file_path)
    else:
        return []

# ============================================================
# Embedding
# ============================================================
def generate_embeddings(chunks: List[Dict]) -> List[Dict]:
    """為所有 chunks 生成 embeddings"""
    total = len(chunks)
    
    for i, chunk in enumerate(chunks):
        # 進度顯示
        if (i + 1) % 10 == 0 or i == 0 or i == total - 1:
            print(f"  Embedding: {i + 1}/{total}", end='\r')
        
        try:
            response = ollama.embeddings(model=EMBEDDING_MODEL, prompt=chunk['content'])
            chunk['embedding'] = response['embedding']
        except Exception as e:
            print(f"\n  [ERROR] Embedding 失敗: {e}")
            chunk['embedding'] = []
    
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

def save_knowledge_base(kb: Dict, output_path: Path):
    """儲存知識庫"""
    # 更新 metadata
    kb["metadata"]["updated_at"] = datetime.now().isoformat()
    kb["metadata"]["total_documents"] = len(kb["metadata"]["documents"])
    kb["metadata"]["total_chunks"] = len(kb["chunks"])
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(kb, f, ensure_ascii=False, indent=2)
    
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
    
    if input_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        print(f"[ERROR] 不支援的檔案類型: {input_path.suffix}")
        print(f"        支援: {', '.join(SUPPORTED_EXTENSIONS)}")
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
# 入口
# ============================================================
if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("用法: python rag_indexer.py <input_file> <output_json>")
        print("")
        print("參數:")
        print("  input_file   要加入的文件 (pdf/md/txt)")
        print("  output_json  知識庫檔案 (不存在則建立，存在則 append)")
        print("")
        print("範例:")
        print("  python rag_indexer.py manual.pdf knowledge.json")
        print("  python rag_indexer.py guide.md knowledge.json")
        print("  python rag_indexer.py notes.txt knowledge.json")
        print("")
        print(f"支援的檔案類型: {', '.join(SUPPORTED_EXTENSIONS)}")
        sys.exit(1)
    
    input_file = sys.argv[1]
    output_file = sys.argv[2]
    
    add_document(input_file, output_file)