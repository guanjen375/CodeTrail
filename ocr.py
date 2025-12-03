#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - OCR 與二進位檔案處理功能
"""

import re
import base64
import requests
from pathlib import Path

from config import OLLAMA_GENERATE_URL, VL_MODEL, IMAGE_EXTENSIONS


# 支援的二進位檔案副檔名
BINARY_EXTENSIONS = {".bin", ".dat", ".raw", ".fw", ".img", ".rom", ".hex"}


def ocr_image(path: str) -> str:
    """對圖片進行 OCR"""
    p = Path(path).expanduser().resolve()

    if not p.exists():
        return f"[OCR 錯誤] 檔案不存在: {path}"

    if p.suffix.lower() not in IMAGE_EXTENSIONS:
        return f"[OCR 錯誤] 不支援的格式: {p.suffix}"

    file_size = p.stat().st_size
    if file_size > 20 * 1024 * 1024:
        return f"[OCR 錯誤] 圖片過大: {file_size / 1024 / 1024:.1f}MB"

    try:
        with open(p, "rb") as f:
            data = base64.b64encode(f.read()).decode()

        resp = requests.post(OLLAMA_GENERATE_URL, json={
            "model": VL_MODEL,
            "prompt": "列出圖片中的所有文字，保持格式。",
            "images": [data],
            "stream": False,
            "options": {"num_ctx": 4096, "temperature": 0.1},
        }, timeout=120)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        return f"[OCR 錯誤] {type(e).__name__}: {e}"


def read_binary(path: str, max_bytes: int = 4096) -> str:
    """讀取二進位檔案並轉換為可分析格式"""
    p = Path(path).expanduser().resolve()

    if not p.exists():
        return f"[BIN 錯誤] 檔案不存在: {path}"

    file_size = p.stat().st_size
    if file_size > 50 * 1024 * 1024:  # 50MB 限制
        return f"[BIN 錯誤] 檔案過大: {file_size / 1024 / 1024:.1f}MB (上限 50MB)"

    try:
        with open(p, "rb") as f:
            data = f.read(max_bytes)

        # 基本資訊
        info = [
            f"檔案: {p.name}",
            f"大小: {file_size:,} bytes",
            f"讀取: 前 {len(data):,} bytes",
        ]

        # Hex dump (前 512 bytes，每行 16 bytes)
        hex_lines = []
        for i in range(0, min(len(data), 512), 16):
            chunk = data[i:i+16]
            hex_part = ' '.join(f'{b:02x}' for b in chunk)
            # ASCII 可視字元
            ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
            hex_lines.append(f"{i:08x}  {hex_part:<48}  |{ascii_part}|")

        # 提取可讀字串 (長度 >= 4)
        strings = []
        current = []
        for b in data:
            if 32 <= b < 127:
                current.append(chr(b))
            else:
                if len(current) >= 4:
                    strings.append(''.join(current))
                current = []
        if len(current) >= 4:
            strings.append(''.join(current))

        # 組合輸出
        result = '\n'.join(info)
        result += '\n\nHex Dump:\n' + '\n'.join(hex_lines)
        if strings:
            result += f'\n\n可讀字串 ({len(strings)} 個):\n' + '\n'.join(strings[:50])
            if len(strings) > 50:
                result += f'\n... (還有 {len(strings) - 50} 個字串)'

        return result

    except Exception as e:
        return f"[BIN 錯誤] {type(e).__name__}: {e}"


def process_binary(text: str) -> tuple[str, str]:
    """處理文字中的二進位檔案引用 (bin:/path/to/file.bin)"""
    pattern = r'bin:([^\s]+)'
    matches = re.findall(pattern, text, re.IGNORECASE)
    clean = re.sub(pattern, '', text, flags=re.IGNORECASE).strip()

    if not matches:
        return text, ""

    ctx = "\n附加二進位檔案:\n"
    for m in matches:
        print(f"[BIN] 讀取: {m}")
        ctx += f"\n[{m}]:\n{read_binary(m)}\n"

    return clean, ctx


def process_images(text: str) -> tuple[str, str]:
    """處理文字中的圖片引用"""
    pattern = r'img:([^\s]+\.(?:png|jpg|jpeg|gif|webp))'
    matches = re.findall(pattern, text, re.IGNORECASE)
    clean = re.sub(pattern, '', text, flags=re.IGNORECASE).strip()

    if not matches:
        return text, ""

    ctx = "\n附加圖片:\n"
    for m in matches:
        print(f"[IMG] OCR: {m}")
        ctx += f"\n[{m}]:\n{ocr_image(m)}\n"

    return clean, ctx
