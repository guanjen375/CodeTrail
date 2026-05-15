"""外部檔案匯入的安全邊界測試。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import config
from external_import import import_external_file


def _enable_import(monkeypatch: pytest.MonkeyPatch, allowed_root: Path) -> None:
    monkeypatch.setattr(config, "EXTERNAL_IMPORT_ENABLED", True)
    monkeypatch.setattr(config, "EXTERNAL_IMPORT_ROOTS", [str(allowed_root)])
    monkeypatch.setattr(config, "EXTERNAL_IMPORT_DEST_DIR", ".aicode_uploads")
    monkeypatch.setattr(config, "EXTERNAL_IMPORT_MAX_BYTES", 1024)
    monkeypatch.setattr(config, "EXTERNAL_IMPORT_ALLOWED_EXTENSIONS", {".png", ".txt", ".pdf", ".bin"})


def test_import_external_file_default_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "project"
    root.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    src = downloads / "error.png"
    src.write_bytes(b"png")

    monkeypatch.setattr(config, "EXTERNAL_IMPORT_ENABLED", False)

    out = import_external_file(str(src), str(root))

    assert "未啟用" in out
    assert not (root / ".aicode_uploads").exists()


def test_import_external_file_copies_allowed_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "project"
    root.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    src = downloads / "error.png"
    src.write_bytes(b"fake-png")
    _enable_import(monkeypatch, downloads)

    out = import_external_file(str(src), str(root))

    dest = root / ".aicode_uploads" / "error.png"
    assert "=== import_external_file" in out
    assert "已匯入: .aicode_uploads/error.png" in out
    assert dest.read_bytes() == b"fake-png"
    assert not (dest.stat().st_mode & 0o111)


def test_import_external_file_rejects_source_outside_allowed_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "project"
    root.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    src = outside / "secret.png"
    src.write_bytes(b"secret")
    _enable_import(monkeypatch, allowed)

    out = import_external_file(str(src), str(root))

    assert "不在允許的匯入來源目錄" in out
    assert not (root / ".aicode_uploads").exists()


def test_import_external_file_rejects_symlink_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "project"
    root.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.png"
    target.write_bytes(b"secret")
    link = allowed / "link.png"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink 在這個平台不支援")
    _enable_import(monkeypatch, allowed)

    out = import_external_file(str(link), str(root))

    assert "不在允許的匯入來源目錄" in out
    assert not (root / ".aicode_uploads").exists()


def test_import_external_file_rejects_unsupported_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "project"
    root.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    src = allowed / "tool.sh"
    src.write_text("echo hi\n", encoding="utf-8")
    _enable_import(monkeypatch, allowed)

    out = import_external_file(str(src), str(root))

    assert "不支援的副檔名" in out
    assert not (root / ".aicode_uploads").exists()


def test_import_external_file_rejects_large_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "project"
    root.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    src = allowed / "big.bin"
    src.write_bytes(b"x" * 2000)
    _enable_import(monkeypatch, allowed)
    monkeypatch.setattr(config, "EXTERNAL_IMPORT_MAX_BYTES", 100)

    out = import_external_file(str(src), str(root))

    assert "檔案太大" in out
    assert not (root / ".aicode_uploads").exists()


def test_import_external_file_rejects_path_dest_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "project"
    root.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    src = allowed / "error.png"
    src.write_bytes(b"png")
    _enable_import(monkeypatch, allowed)

    out = import_external_file(str(src), str(root), dest_name="../evil.png")

    assert "dest_name" in out
    assert not (root / ".aicode_uploads").exists()


def test_import_external_file_avoids_overwriting_existing_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "project"
    upload_dir = root / ".aicode_uploads"
    upload_dir.mkdir(parents=True)
    (upload_dir / "log.txt").write_text("old\n", encoding="utf-8")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    src = allowed / "log.txt"
    src.write_text("new\n", encoding="utf-8")
    _enable_import(monkeypatch, allowed)

    out = import_external_file(str(src), str(root))

    assert "已匯入: .aicode_uploads/log_1.txt" in out
    assert (upload_dir / "log.txt").read_text(encoding="utf-8") == "old\n"
    assert (upload_dir / "log_1.txt").read_text(encoding="utf-8") == "new\n"


def test_import_external_file_reports_already_inside_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "project"
    root.mkdir()
    src = root / "logs" / "build.txt"
    src.parent.mkdir()
    src.write_text("inside\n", encoding="utf-8")
    _enable_import(monkeypatch, tmp_path / "allowed")

    out = import_external_file(str(src), str(root))

    assert "已在 AICODE_ROOT 內" in out
    assert "logs/build.txt" in out
