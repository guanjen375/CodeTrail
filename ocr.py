#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - OCR 功能
"""

import re
import base64
import requests
from pathlib import Path

from config import OLLAMA_GENERATE_URL, VL_MODEL, IMAGE_EXTENSIONS


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
