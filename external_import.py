#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""受控外部檔案匯入。

MCP server 的一般工具只允許讀 AICODE_ROOT。這個模組提供唯一的外部入口：
從明確允許的來源目錄複製檔案到 AICODE_ROOT/.aicode_uploads/，後續仍交給
read_file/analyze_file/ingest_document 這些 sandbox 工具處理。
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Optional

import config


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _default_import_roots() -> list[Path]:
    roots: list[Path] = []
    home = os.environ.get("HOME")
    if home:
        roots.append(Path(home).expanduser() / "Downloads")
    roots.append(Path("/tmp"))
    return roots


def _configured_import_roots() -> list[Path]:
    raw_roots = getattr(config, "EXTERNAL_IMPORT_ROOTS", [])
    roots = [Path(p).expanduser() for p in raw_roots] if raw_roots else _default_import_roots()

    resolved: list[Path] = []
    for root in roots:
        try:
            p = root.resolve()
        except (OSError, ValueError):
            continue
        if p.is_dir() and p not in resolved:
            resolved.append(p)
    return resolved


def _format_roots(roots: list[Path]) -> str:
    if not roots:
        return "(沒有可用來源目錄)"
    return "\n".join(f"  - {p}" for p in roots)


def _safe_dest_name(name: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", name.strip())
    cleaned = cleaned.strip("._")
    if not cleaned:
        cleaned = "upload"
    if cleaned.startswith("."):
        cleaned = "upload_" + cleaned.lstrip(".")
    return cleaned[:160]


def _next_available_path(dest_dir: Path, filename: str) -> Path:
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate

    suffix = candidate.suffix
    stem = candidate.stem or "upload"
    for idx in range(1, 1000):
        candidate = dest_dir / f"{stem}_{idx}{suffix}"
        if not candidate.exists():
            return candidate
    raise OSError("無法產生不衝突的匯入檔名")


def _validate_dest_name(dest_name: Optional[str]) -> tuple[str | None, str | None]:
    if dest_name is None:
        return None, None
    name = dest_name.strip().strip('"').strip("'")
    if not name:
        return None, "錯誤: dest_name 不可為空"
    if "/" in name or "\\" in name or Path(name).name != name:
        return None, "錯誤: dest_name 只能是檔名，不能包含目錄或路徑分隔符"
    return name, None


def import_external_file(source_path: str, aicode_root: str, dest_name: Optional[str] = None) -> str:
    """Copy an allowed external file into AICODE_ROOT and return the new relative path."""
    if not getattr(config, "EXTERNAL_IMPORT_ENABLED", False):
        return (
            "錯誤: 外部檔案匯入未啟用。\n"
            "請用 AI_CODE_ALLOW_EXTERNAL_IMPORT=1 啟動 aicode；若來源不在 ~/Downloads 或 /tmp，"
            "再設定 AI_CODE_IMPORT_ROOTS。"
        )

    raw_source = (source_path or "").strip().strip('"').strip("'")
    if not raw_source:
        return "錯誤: source_path 不可為空"

    try:
        root = Path(aicode_root).resolve()
        src = Path(raw_source).expanduser().resolve()
    except (OSError, ValueError) as e:
        return f"錯誤: 路徑無法解析: {e}"

    if not root.is_dir():
        return f"錯誤: AICODE_ROOT 不是目錄: {root}"

    if not src.is_file():
        return f"錯誤: 外部檔案不存在或不是一般檔案: {src}"

    if _inside(src, root):
        rel = src.relative_to(root).as_posix()
        return (
            "檔案已在 AICODE_ROOT 內，不需要匯入。\n"
            f"可直接使用: {rel}"
        )

    allowed_roots = _configured_import_roots()
    if not any(_inside(src, allowed_root) for allowed_root in allowed_roots):
        return (
            "錯誤: 來源檔案不在允許的匯入來源目錄內。\n"
            f"來源: {src}\n"
            "目前允許來源:\n"
            f"{_format_roots(allowed_roots)}\n"
            "可用 AI_CODE_IMPORT_ROOTS 設定額外來源目錄。"
        )

    allowed_ext = {e.lower() for e in getattr(config, "EXTERNAL_IMPORT_ALLOWED_EXTENSIONS", set())}
    source_ext = src.suffix.lower()
    if allowed_ext and source_ext not in allowed_ext:
        return (
            f"錯誤: 不支援的副檔名 {source_ext or '(無副檔名)'}。\n"
            f"允許副檔名: {sorted(allowed_ext)}"
        )

    try:
        size = src.stat().st_size
    except OSError as e:
        return f"錯誤: 無法讀取來源檔案資訊: {e}"

    max_bytes = int(getattr(config, "EXTERNAL_IMPORT_MAX_BYTES", 0))
    if max_bytes > 0 and size > max_bytes:
        return (
            f"錯誤: 檔案太大 ({size:,} bytes)，上限是 {max_bytes:,} bytes。\n"
            "可用 AI_CODE_EXTERNAL_IMPORT_MAX_MB 調整，但不要把大型敏感資料整包匯入。"
        )

    explicit_name, name_error = _validate_dest_name(dest_name)
    if name_error:
        return name_error
    filename = _safe_dest_name(explicit_name or src.name)
    dest_ext = Path(filename).suffix.lower()
    if allowed_ext and dest_ext not in allowed_ext:
        return (
            f"錯誤: 匯入後檔名副檔名不支援: {dest_ext or '(無副檔名)'}。\n"
            f"允許副檔名: {sorted(allowed_ext)}"
        )

    dest_dir = (root / getattr(config, "EXTERNAL_IMPORT_DEST_DIR", ".aicode_uploads")).resolve()
    if not _inside(dest_dir, root):
        return "錯誤: EXTERNAL_IMPORT_DEST_DIR 必須位於 AICODE_ROOT 內"

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            dest_dir.chmod(0o700)
        except OSError:
            pass
        dest = _next_available_path(dest_dir, filename)
        shutil.copyfile(src, dest)
        try:
            dest.chmod(0o600)
        except OSError:
            pass
    except OSError as e:
        return f"錯誤: 匯入失敗: {e}"

    rel = dest.relative_to(root).as_posix()
    return (
        "=== import_external_file ✓ ===\n"
        f"來源: {src}\n"
        f"已匯入: {rel} ({size:,} bytes)\n\n"
        "下一步:\n"
        f"- 圖片 / ELF / firmware: analyze_file('{rel}')\n"
        f"- PDF / Markdown / TXT: ingest_document('{rel}') 後 reload_knowledge_base()\n"
        f"- log / text: read_file('{rel}')"
    )
