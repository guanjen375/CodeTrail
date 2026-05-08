"""mcp_server.py 的 AICODE_ROOT 安全檢查。

直接從 mcp_server import 純函式 _validate_aicode_root,避免啟動 FastMCP
(FastMCP 會綁 stdio,測試環境不適合)。
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _import_validator():
    """從 mcp_server.py 抓 _validate_aicode_root,但不執行 module 主體。

    mcp_server 啟動時會做 sys.exit / 連 KB / load FastMCP,跑單純的 import
    會炸。這裡用 ast 切出函式,exec 在隔離 namespace 裡。
    """
    src = (REPO_ROOT / "mcp_server.py").read_text(encoding="utf-8")
    import ast
    tree = ast.parse(src)
    func = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == "_validate_aicode_root"),
        None,
    )
    assert func is not None, "_validate_aicode_root 不在 mcp_server.py — root safety 檢查被砍了？"
    module = ast.Module(body=[func], type_ignores=[])
    ns: dict = {"Path": Path}
    exec(compile(module, "mcp_server.py", "exec"), ns)
    return ns["_validate_aicode_root"]


_validate = _import_validator()


def test_rejects_empty_root():
    resolved, err = _validate(None, "/home/x", allow_home_override=False)
    assert resolved is None
    assert err and "AICODE_ROOT" in err


def test_rejects_root_slash():
    resolved, err = _validate("/", "/home/x", allow_home_override=False)
    assert resolved is None
    assert err and "/" in err


def test_rejects_home(tmp_path: Path):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    resolved, err = _validate(str(fake_home), str(fake_home), allow_home_override=False)
    assert resolved is None
    assert err and "$HOME" in err


def test_allows_home_when_overridden(tmp_path: Path):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    resolved, err = _validate(str(fake_home), str(fake_home), allow_home_override=True)
    assert err is None
    assert resolved == str(fake_home.resolve())


def test_allows_normal_subdir(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    resolved, err = _validate(str(project), str(tmp_path), allow_home_override=False)
    assert err is None
    assert resolved == str(project.resolve())


def test_rejects_nonexistent_dir(tmp_path: Path):
    nope = tmp_path / "nope"
    resolved, err = _validate(str(nope), "/home/x", allow_home_override=False)
    assert resolved is None
    assert err and "不是目錄" in err
