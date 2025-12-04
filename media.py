#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 媒體檔案處理
- 圖片 OCR（使用 VL 模型）
- 二進位檔案分析（Hex dump + strings 提取）
"""

import re
import base64
from pathlib import Path
from typing import Optional

from http_client import get_session
from config import OLLAMA_GENERATE_URL, VL_MODEL, IMAGE_EXTENSIONS


# 支援的二進位檔案副檔名
BINARY_EXTENSIONS = {".bin", ".dat", ".raw", ".fw", ".img", ".rom", ".hex"}

# 全域 sandbox root（由 main.py 設定）
_SANDBOX_ROOT: Optional[Path] = None
_ALLOW_EXTERNAL: bool = False


def set_sandbox_root(root: str, allow_external: bool = False) -> None:
    """設定 sandbox 根目錄，只允許讀取此目錄內的檔案

    Args:
        root: sandbox 根目錄
        allow_external: 是否允許讀取外部的圖片和 bin 檔案（預設 False）
    """
    global _SANDBOX_ROOT, _ALLOW_EXTERNAL
    _SANDBOX_ROOT = Path(root).resolve()
    _ALLOW_EXTERNAL = allow_external


def _safe_path(path: str, allow_external: bool = False, allowed_extensions: set = None) -> Optional[Path]:
    """驗證路徑是否在 sandbox 內，防止讀取任意本機檔案

    相對路徑會以 _SANDBOX_ROOT 為基準解析，而非當前工作目錄

    Args:
        path: 檔案路徑
        allow_external: 是否允許外部路徑
        allowed_extensions: 允許的外部檔案副檔名（None 表示不限制）
    """
    if _SANDBOX_ROOT is None:
        # 未設定 sandbox 時，拒絕所有請求
        return None

    try:
        p = Path(path).expanduser()
        # 相對路徑以 sandbox root 為基準，絕對路徑直接使用
        if not p.is_absolute():
            full = (_SANDBOX_ROOT / p).resolve()
        else:
            full = p.resolve()

        # 檢查是否在 sandbox 內
        try:
            full.relative_to(_SANDBOX_ROOT)
            return full
        except ValueError:
            # 路徑在 sandbox 外
            if allow_external and _ALLOW_EXTERNAL and full.exists():
                # 檢查副檔名（如果有限制）
                if allowed_extensions is None or full.suffix.lower() in allowed_extensions:
                    return full
            return None
    except Exception:
        return None


def ocr_image(path: str) -> str:
    """對圖片進行 OCR"""
    p = _safe_path(path, allow_external=True, allowed_extensions=IMAGE_EXTENSIONS)

    if p is None:
        if _ALLOW_EXTERNAL:
            return f"[OCR 錯誤] 檔案不存在或不是支援的圖片格式: {path}"
        else:
            return f"[OCR 錯誤] 路徑不在專案目錄內（使用 --allow-external 允許外部檔案）: {path}"

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

        session = get_session()
        resp = session.post(OLLAMA_GENERATE_URL, json={
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


def read_binary(path: str, max_hex_bytes: int = 16384, max_strings: int = 200) -> str:
    """讀取二進位檔案並轉換為可分析格式

    使用 strings 提取整個檔案的可讀字串，並只讀取前 N bytes 做 hex dump
    """
    import subprocess

    # bin 檔案允許使用外部路徑（如果 _ALLOW_EXTERNAL 已啟用）
    p = _safe_path(path, allow_external=True, allowed_extensions=BINARY_EXTENSIONS)

    if p is None:
        if _ALLOW_EXTERNAL:
            return f"[BIN 錯誤] 檔案不存在或不是支援的二進位格式: {path}"
        else:
            return f"[BIN 錯誤] 路徑不在專案目錄內（使用 --allow-external 允許外部檔案）: {path}"

    if not p.exists():
        return f"[BIN 錯誤] 檔案不存在: {path}"

    file_size = p.stat().st_size
    if file_size > 50 * 1024 * 1024:  # 50MB 限制
        return f"[BIN 錯誤] 檔案過大: {file_size / 1024 / 1024:.1f}MB (上限 50MB)"

    try:
        # 基本資訊
        info = [
            f"檔案: {p.name}",
            f"大小: {file_size:,} bytes",
        ]

        # Hex dump (前 1024 bytes，每行 16 bytes) - 用於分析 header/magic
        with open(p, "rb") as f:
            header_data = f.read(1024)

        hex_lines = []
        for i in range(0, len(header_data), 16):
            chunk = header_data[i:i+16]
            hex_part = ' '.join(f'{b:02x}' for b in chunk)
            ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
            hex_lines.append(f"{i:08x}  {hex_part:<48}  |{ascii_part}|")

        # 使用 strings 命令提取整個檔案的可讀字串（長度 >= 6）
        try:
            result_strings = subprocess.run(
                ['strings', '-n', '6', str(p)],
                capture_output=True,
                text=True,
                timeout=30
            )
            all_strings = result_strings.stdout.strip().split('\n') if result_strings.stdout else []
        except (subprocess.TimeoutExpired, FileNotFoundError):
            # fallback: 手動提取前 max_hex_bytes 的字串
            with open(p, "rb") as f:
                data = f.read(max_hex_bytes)
            all_strings = []
            current = []
            for b in data:
                if 32 <= b < 127:
                    current.append(chr(b))
                else:
                    if len(current) >= 6:
                        all_strings.append(''.join(current))
                    current = []
            if len(current) >= 6:
                all_strings.append(''.join(current))

        # 過濾有意義的字串（排除太短或純數字/符號的）
        meaningful_strings = []
        for s in all_strings:
            s = s.strip()
            if len(s) < 6:
                continue
            # 至少包含一個字母
            if any(c.isalpha() for c in s):
                meaningful_strings.append(s)

        # 組合輸出
        result = '\n'.join(info)
        result += '\n\nHex Dump (前 1KB，用於識別檔案格式):\n' + '\n'.join(hex_lines)

        if meaningful_strings:
            # 優先顯示可能的版本/日期資訊
            high_priority = []  # 非常重要：版本號、編譯日期
            medium_priority = []  # 中等重要：boot 相關
            normal_strings = []

            for s in meaningful_strings:
                s_lower = s.lower()
                # 高優先：明確的版本號格式
                if any(kw in s_lower for kw in ['version', '2024', '2025', '2023', 'compiled', 'gcc', 'clang', 'built']):
                    high_priority.append(s)
                elif any(kw in s for kw in ['U-Boot', 'u-boot']):
                    high_priority.append(s)
                # 中優先：boot 相關但不是版本
                elif 'boot' in s_lower:
                    medium_priority.append(s)
                else:
                    normal_strings.append(s)

            result += f'\n\n可讀字串（整個檔案，共 {len(meaningful_strings)} 個）:\n'

            if high_priority:
                result += '\n[最重要 - 版本/編譯資訊]:\n'
                for s in high_priority[:50]:
                    result += f'  {s}\n'

            if medium_priority:
                result += '\n[Boot 相關]:\n'
                for s in medium_priority[:20]:
                    result += f'  {s}\n'
                if len(medium_priority) > 20:
                    result += f'  ... (還有 {len(medium_priority) - 20} 個)\n'

            result += '\n[其他字串]:\n'
            shown = 0
            max_normal = max_strings - len(high_priority) - min(len(medium_priority), 20)
            for s in normal_strings:
                if shown >= max_normal:
                    result += f'... (還有 {len(normal_strings) - shown} 個字串)\n'
                    break
                result += f'  {s}\n'
                shown += 1

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

    ctx = """
╔══════════════════════════════════════════════════════════════╗
║  ⚠️  [BIN] 二進位檔案分析 - 最高優先級                          ║
╚══════════════════════════════════════════════════════════════╝

【強制規則 - 違反將導致回答錯誤】
1. 必須首先分析下方的 Hex dump 和可讀字串
2. 回答必須明確說明「在 BIN 中找到 XXX」或「在 BIN 中沒有找到 XXX」
3. 只有當 BIN 中確實沒有相關資訊時，才能參考程式碼或文件
4. 這些檔案的重要性 > 程式碼 > 一般文件

"""
    for m in matches:
        print(f"[BIN] 讀取: {m}")
        ctx += f"\n[BIN: {m}]\n{read_binary(m)}\n"

    ctx += "\n=== 二進位檔案分析結束 ===\n"
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
