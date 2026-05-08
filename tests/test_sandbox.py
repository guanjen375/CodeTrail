"""Sandbox path containment tests.

涵蓋兩個獨立 sandbox 實作：
- agent_tools.ToolExecutor._safe_path（read_file / grep / patch / list_dir 全部走這條）
- media._safe_path（圖片/ELF/binary 分析走這條，預設 allow_external=True）
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import media
from agent_tools import ToolExecutor


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    (tmp_path / "inside.txt").write_text("hello\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested\n", encoding="utf-8")
    return tmp_path


def test_safe_path_accepts_relative_inside(sandbox: Path):
    ex = ToolExecutor(str(sandbox))
    assert ex._safe_path("inside.txt") is not None
    assert ex._safe_path("sub/nested.txt") is not None
    assert ex._safe_path(".") is not None


def test_safe_path_rejects_dotdot_escape(sandbox: Path):
    ex = ToolExecutor(str(sandbox))
    assert ex._safe_path("../etc/passwd") is None
    assert ex._safe_path("../../tmp") is None
    assert ex._safe_path("sub/../../etc/passwd") is None


def test_safe_path_rejects_absolute_outside(sandbox: Path, tmp_path_factory):
    ex = ToolExecutor(str(sandbox))
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    assert ex._safe_path(str(outside / "secret.txt")) is None


def test_safe_path_rejects_symlink_escape(sandbox: Path, tmp_path_factory):
    """Symlink 指向 sandbox 外應該被拒絕（因為 .resolve() 會解析 symlink）。"""
    ex = ToolExecutor(str(sandbox))
    outside = tmp_path_factory.mktemp("outside_link")
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = sandbox / "evil_link.txt"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink 在這個平台不支援")
    assert ex._safe_path("evil_link.txt") is None


def test_media_safe_path_requires_root(sandbox: Path):
    media._SANDBOX_ROOT = None  # 重置
    media._ALLOW_EXTERNAL = True
    assert media._safe_path("anything.png") is None


def test_media_safe_path_inside_root(sandbox: Path):
    media.set_sandbox_root(str(sandbox), allow_external=False)
    f = sandbox / "img.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    p = media._safe_path("img.png", allow_external=True, allowed_extensions={".png"})
    assert p is not None
    assert p.name == "img.png"


def test_media_safe_path_blocks_external_when_disabled(sandbox: Path, tmp_path_factory):
    media.set_sandbox_root(str(sandbox), allow_external=False)
    out = tmp_path_factory.mktemp("ext")
    f = out / "img.png"
    f.write_bytes(b"\x89PNG")
    # 即使函式呼叫帶 allow_external=True，全域 _ALLOW_EXTERNAL=False 也要擋
    p = media._safe_path(str(f), allow_external=True, allowed_extensions={".png"})
    assert p is None
