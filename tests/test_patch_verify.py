"""apply_patch 套用後的自動驗證(workflow C):同 process、無 subprocess、唯讀的 syntax check。

守的契約:
  1. 核准 apply_patch 不會暗中擴張成命令執行核准——verifier 不 spawn 任何東西,
     不呼叫 run_lint / run_command,也不呼叫會改檔的 formatter。
  2. 三態誠實:passed / failed / skipped;有 skipped 就是「驗證不完整」,絕不把
     「工具缺席」「suffix 不支援」「grammar 沒載入」渲染成通過。
  3. syntax 是寫入後的 advisory gate:失敗不回滾,標題明講「patch 已套用、未回滾」。
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any

import pytest

import config
import container_runner
from agent_tools import ToolExecutor

REPO_ROOT = Path(__file__).resolve().parent.parent

# smoke:安全層(AGENTS.md §1.1 第 2 款「無聲失敗風險的契約」)。
pytestmark = pytest.mark.smoke

TITLE_PASSED = "✓ 驗證完成且通過（requested: syntax）"
TITLE_FAILED = "✗ 驗證未通過——patch 已套用、未回滾（syntax 是 advisory gate）"
TITLE_DISABLED = (
    "⚠ 驗證不完整（skipped: auto verification disabled, PATCH_AUTO_VERIFY=False）"
    "——patch 已套用、未回滾"
)
SECTION_SYNTAX = "=== 自動驗證 (requested: syntax) ==="
NEXT_STEPS = "建議下一步: 對改過的檔案呼叫 codetrail_run_lint(fix=False)"


@pytest.fixture
def runner(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "PATCH_ENABLED", True)
    # 故意把 run_command 開著:證明 verifier 就算能用也不會去用它。
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", True)
    monkeypatch.setattr(container_runner, "CONTAINER_ENABLED", False)
    monkeypatch.setattr(config, "PATCH_AUTO_VERIFY", True)
    monkeypatch.setattr(config, "PATCH_VERIFY_STEPS", ["syntax"])
    return ToolExecutor(str(tmp_path))


@pytest.fixture
def spawn_guard(monkeypatch) -> list:
    """任何 subprocess 都先記錄再炸;verifier 若吞例外,記錄仍會揭穿它。"""
    calls: list = []

    def boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("subprocess spawned by apply_patch verification")

    monkeypatch.setattr("agent_tools.subprocess.run", boom)
    monkeypatch.setattr("agent_tools.subprocess.Popen", boom)
    return calls


def _patch(name: str, old: str, new: str) -> str:
    return f"--- a/{name}\n+++ b/{name}\n@@\n-{old}\n+{new}\n"


def _apply(runner: ToolExecutor, root: Path, name: str, old: str, new: str) -> str:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(old + "\n", encoding="utf-8")
    return runner.apply_patch(_patch(name, old, new))


def _verify_lines(out: str) -> list[str]:
    """apply_patch 結果中、驗證區塊的行(從三態標題行開始)。"""
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(("✓ 驗證", "⚠ 驗證", "✗ 驗證")):
            return lines[i:]
    raise AssertionError(f"結果裡沒有三態驗證標題:\n{out}")


# ---------------------------------------------------------------------------
# config 預設與「不 spawn」契約
# ---------------------------------------------------------------------------
def test_default_config_is_syntax_only():
    assert config.PATCH_AUTO_VERIFY is True
    assert config.PATCH_VERIFY_STEPS == ["syntax"]


def test_auto_verify_false_runs_no_syntax_check(runner: ToolExecutor, tmp_path: Path,
                                                monkeypatch, spawn_guard):
    import patch_verify

    monkeypatch.setattr(config, "PATCH_AUTO_VERIFY", False)
    touched: list[str] = []

    def boom(*args, **kwargs):
        touched.append("called")
        raise AssertionError("PATCH_AUTO_VERIFY=False 時不得執行任何檢查")

    monkeypatch.setattr(patch_verify, "verify_files", boom)
    monkeypatch.setattr(patch_verify, "check_syntax", boom)
    out = _apply(runner, tmp_path, "x.py", "x = 1", "x = 2")
    assert (tmp_path / "x.py").read_text(encoding="utf-8") == "x = 2\n"
    lines = _verify_lines(out)
    assert lines[0] == TITLE_DISABLED
    assert lines[1] == "=== 自動驗證 ==="
    assert lines[2] == "○ 未執行任何檢查（PATCH_AUTO_VERIFY=False）"
    assert lines[-1].startswith(NEXT_STEPS)
    assert touched == [] and spawn_guard == []


def test_auto_verify_false_never_touches_safe_path_or_parser(runner: ToolExecutor, tmp_path: Path,
                                                              monkeypatch, spawn_guard):
    """停用分支連 stat / read 都不做:_safe_path 零呼叫(處置 #10)。"""
    import patch_verify

    monkeypatch.setattr(config, "PATCH_AUTO_VERIFY", False)
    (tmp_path / "x.py").write_text("x = 1\n", encoding="utf-8")
    touched: list[str] = []

    def boom(*args, **kwargs):
        touched.append(str(args[:1]))
        raise AssertionError("PATCH_AUTO_VERIFY=False 時不得碰檔案或 parser")

    monkeypatch.setattr(runner, "_safe_path", boom)
    monkeypatch.setattr(patch_verify, "verify_files", boom)
    monkeypatch.setattr(patch_verify, "check_syntax", boom)
    lines = runner._verify_patched_files(["x.py"])
    assert lines[0] == TITLE_DISABLED
    assert lines[-1].startswith(NEXT_STEPS)
    assert touched == [] and spawn_guard == []


def test_auto_verify_true_spawns_no_subprocess(runner: ToolExecutor, tmp_path: Path, spawn_guard):
    out = _apply(runner, tmp_path, "x.py", "x = 1", "x = 2")
    lines = _verify_lines(out)
    assert lines[0] == TITLE_PASSED
    assert lines[1] == SECTION_SYNTAX
    assert "✓ x.py: syntax passed" in lines
    assert lines[-1].startswith(NEXT_STEPS)
    assert spawn_guard == [], f"verifier spawned: {spawn_guard}"


def test_legacy_steps_are_not_consumed_and_do_not_spawn(runner: ToolExecutor, tmp_path: Path,
                                                       monkeypatch, spawn_guard):
    monkeypatch.setattr(config, "PATCH_VERIFY_STEPS", ["lint", "typecheck", "test"])
    lint_calls: list = []
    cmd_calls: list = []
    monkeypatch.setattr(runner, "run_lint", lambda *a, **k: lint_calls.append(a) or "✓ stub")
    monkeypatch.setattr(runner, "run_command", lambda *a, **k: cmd_calls.append(a) or "=== ✓ 成功 ===")
    out = _apply(runner, tmp_path, "x.py", "x = 1", "x = 2")
    lines = _verify_lines(out)
    assert lines[0] == "⚠ 驗證不完整（skipped: 3/3）——patch 已套用、未回滾"
    assert lines[1] == "=== 自動驗證 (requested: lint, typecheck, test) ==="
    for step in ("lint", "typecheck", "test"):
        assert (
            f"○ x.py: {step} skipped: step '{step}' is no longer consumed by apply_patch; "
            "call codetrail_run_lint(fix=False) / codetrail_run_command explicitly"
        ) in lines, lines
    assert lint_calls == [] and cmd_calls == [] and spawn_guard == []


def test_unknown_step_name_is_reported_unsupported(runner: ToolExecutor, tmp_path: Path,
                                                   monkeypatch, spawn_guard):
    monkeypatch.setattr(config, "PATCH_VERIFY_STEPS", ["fuzz"])
    out = _apply(runner, tmp_path, "x.py", "x = 1", "x = 2")
    lines = _verify_lines(out)
    assert lines[0] == "⚠ 驗證不完整（skipped: 1/1）——patch 已套用、未回滾"
    assert "○ x.py: fuzz skipped: unsupported verification step 'fuzz'" in lines


def test_verify_block_never_calls_run_lint_or_run_command(runner: ToolExecutor, tmp_path: Path,
                                                          monkeypatch, spawn_guard):
    calls: list = []

    def boom(*args, **kwargs):
        calls.append(args)
        raise AssertionError("verifier 不得呼叫 run_lint / run_command")

    monkeypatch.setattr(runner, "run_lint", boom)
    monkeypatch.setattr(runner, "run_command", boom)
    out = _apply(runner, tmp_path, "x.py", "x = 1", "x = 2")
    assert _verify_lines(out)[0] == TITLE_PASSED
    assert calls == [] and spawn_guard == []


_FORBIDDEN_CALL_NAMES = {
    "subprocess", "system", "popen", "spawn", "spawnl", "spawnle", "spawnlp", "spawnlpe",
    "spawnv", "spawnve", "spawnvp", "spawnvpe", "execv", "execve", "execvp", "execvpe",
    "execl", "execle", "execlp", "execlpe", "fork", "forkpty", "posix_spawn", "posix_spawnp",
    "create_subprocess_exec", "create_subprocess_shell", "multiprocessing",
    "ProcessPoolExecutor", "Popen", "run", "call", "check_call", "check_output",
}
_IMPORT_ALLOWLIST = {"ast", "dataclasses", "pathlib", "typing", "ast_parser"}


def test_patch_verify_module_import_allowlist_is_exact():
    """no-hidden-subprocess 的靜態守門:import 集合精確等於 allowlist,且沒有任何可 spawn 的呼叫。"""
    source = (REPO_ROOT / "patch_verify.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported == _IMPORT_ALLOWLIST, imported
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in _FORBIDDEN_CALL_NAMES:
                offenders.append(f"line {node.lineno}: {name}")
    assert offenders == [], offenders


def test_native_and_executor_docstrings_are_updated():
    """T2 擁有的三處說明不能留下反向敘述(review BLOCKER #11)。"""
    src = (REPO_ROOT / "agent_tools.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    executor = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ToolExecutor")
    docs = {
        n.name: (ast.get_docstring(n) or "")
        for n in executor.body if isinstance(n, ast.FunctionDef)
    }
    run_command_doc = " ".join(docs["run_command"].split())
    assert "1..600" in run_command_doc
    assert "只在 AI_CODE_ENABLE_BUILD_COMMANDS=1" in run_command_doc
    assert "git 不在白名單" in run_command_doc
    assert "client 可能更早截止" in run_command_doc
    verify_doc = " ".join(docs["_verify_patched_files"].split())
    assert "codetrail_run_lint(fix=False)" in verify_doc
    assert "syntax check" in verify_doc
    config_src = (REPO_ROOT / "config.py").read_text(encoding="utf-8")
    assert "供 Patch 驗證使用" not in config_src
    assert "apply_patch 不再自動執行" in config_src


# ---------------------------------------------------------------------------
# 三態:suffix / python / cython / C-C++
# ---------------------------------------------------------------------------
def test_unsupported_suffix_is_skipped_and_reported_incomplete(runner: ToolExecutor,
                                                              tmp_path: Path, spawn_guard):
    out = _apply(runner, tmp_path, "notes.txt", "old", "new")
    lines = _verify_lines(out)
    assert lines[0] == "⚠ 驗證不完整（skipped: 1/1）——patch 已套用、未回滾"
    assert "○ notes.txt: syntax skipped: parser unavailable for .txt" in lines
    assert "所有驗證通過" not in out
    assert "驗證完成且通過" not in out


def test_python_syntax_error_is_failed_and_patch_stays_applied(runner: ToolExecutor,
                                                              tmp_path: Path, spawn_guard):
    out = _apply(runner, tmp_path, "x.py", "def f(): pass", "def f(:")
    assert (tmp_path / "x.py").read_text(encoding="utf-8") == "def f(:\n"
    lines = _verify_lines(out)
    assert lines[0] == TITLE_FAILED
    assert "✗ x.py: syntax failed" in lines
    assert any(line.startswith("  x.py:1: ") for line in lines), lines


@pytest.mark.parametrize("suffix", [".py", ".pyi"])
def test_python_family_ok_is_passed(runner: ToolExecutor, tmp_path: Path, spawn_guard, suffix):
    out = _apply(runner, tmp_path, f"mod{suffix}", "x: int = 1", "x: int = 2")
    lines = _verify_lines(out)
    assert lines[0] == TITLE_PASSED
    assert f"✓ mod{suffix}: syntax passed" in lines


def test_pyx_is_skipped_not_parsed_as_python(runner: ToolExecutor, tmp_path: Path, spawn_guard):
    out = _apply(runner, tmp_path, "fast.pyx", "cdef int x = 1", "cdef int x = 2")
    lines = _verify_lines(out)
    assert lines[0] == "⚠ 驗證不完整（skipped: 1/1）——patch 已套用、未回滾"
    assert "○ fast.pyx: syntax skipped: Cython source; python ast not applicable" in lines


@pytest.mark.parametrize("suffix,expected_lang", [
    (".c", "c"), (".cpp", "cpp"), (".cc", "cpp"), (".cxx", "cpp"),
    (".hpp", "cpp"), (".hh", "cpp"), (".hxx", "cpp"),
])
def test_c_family_suffix_maps_to_grammar(monkeypatch, suffix, expected_lang):
    import ast_parser
    import patch_verify

    asked: list[str] = []
    monkeypatch.setattr(ast_parser, "_try_load_tree_sitter_language",
                        lambda lang: asked.append(lang) or None)
    result = patch_verify.check_syntax(f"src/unit{suffix}", b"int x;\n")
    assert asked == [expected_lang]
    assert result.status == "skipped"
    assert result.reason == f"parser unavailable (tree-sitter grammar '{expected_lang}' not loaded)"


@pytest.mark.parametrize("h_lang", ["c", "cpp"])
def test_dot_h_follows_header_language_decision(monkeypatch, h_lang):
    import ast_parser
    import patch_verify

    monkeypatch.setenv("AICODE_H_LANG", h_lang)
    asked: list[str] = []
    monkeypatch.setattr(ast_parser, "_try_load_tree_sitter_language",
                        lambda lang: asked.append(lang) or None)
    result = patch_verify.check_syntax("inc/board.h", b"int x;\n")
    assert asked == [h_lang]
    assert result.status == "skipped"


def test_missing_grammar_is_skipped_not_passed(runner: ToolExecutor, tmp_path: Path,
                                              monkeypatch, spawn_guard):
    import ast_parser

    monkeypatch.setattr(ast_parser, "_try_load_tree_sitter_language", lambda lang: None)
    out = _apply(runner, tmp_path, "main.c", "int x = 1;", "int x = 2;")
    lines = _verify_lines(out)
    assert lines[0] == "⚠ 驗證不完整（skipped: 1/1）——patch 已套用、未回滾"
    assert "○ main.c: syntax skipped: parser unavailable (tree-sitter grammar 'c' not loaded)" in lines


def _real_c_grammar_available() -> bool:
    import ast_parser

    return ast_parser._try_load_tree_sitter_language("c") is not None


@pytest.mark.skipif(not _real_c_grammar_available(), reason="tree-sitter C grammar not loaded")
def test_tree_sitter_error_node_is_failed(runner: ToolExecutor, tmp_path: Path, spawn_guard):
    out = _apply(runner, tmp_path, "main.c", "int main(void) { return 1; }", "int main( { return 1; }")
    lines = _verify_lines(out)
    assert lines[0] == TITLE_FAILED
    assert "✗ main.c: syntax failed" in lines
    assert any(line.startswith("  main.c:1: syntax error") for line in lines), lines


@pytest.mark.skipif(not _real_c_grammar_available(), reason="tree-sitter C grammar not loaded")
def test_tree_sitter_missing_node_is_failed(runner: ToolExecutor, tmp_path: Path, spawn_guard):
    out = _apply(runner, tmp_path, "main.c", "int main(void) { return 1; }", "int main(void) { return 1 }")
    lines = _verify_lines(out)
    assert lines[0] == TITLE_FAILED
    assert any(line.startswith("  main.c:1: missing ;") for line in lines), lines


class _FakeNode:
    def __init__(self, type_: str, *, is_error=False, is_missing=False, row=0,
                 children=(), text=b"", has_error=None):
        self.type = type_
        self.is_error = is_error
        self.is_missing = is_missing
        self.start_point = (row, 0)
        self.children = list(children)
        self.text = text
        self._has_error = has_error

    @property
    def has_error(self):
        if self._has_error is not None:
            return self._has_error
        return self.is_error or self.is_missing or any(c.has_error for c in self.children)


def _install_fake_parser(monkeypatch, root: _FakeNode):
    import ast_parser

    class FakeTree:
        root_node = root

    class FakeParser:
        def __init__(self, language):
            self.language = language

        def parse(self, data):
            return FakeTree()

    monkeypatch.setattr(ast_parser, "_try_load_tree_sitter_language", lambda lang: object())
    monkeypatch.setattr(ast_parser, "Parser", FakeParser)


def test_fake_tree_error_and_missing_are_both_failed(monkeypatch):
    import patch_verify

    root = _FakeNode("translation_unit", children=[
        _FakeNode("ERROR", is_error=True, row=2, text=b"int main( {"),
        _FakeNode(";", is_missing=True, row=4),
    ])
    _install_fake_parser(monkeypatch, root)
    result = patch_verify.check_syntax("src/a.c", b"...")
    assert result.status == "failed"
    assert [(d.line, d.message.split(" ")[0]) for d in result.diagnostics] == [(3, "syntax"), (5, "missing")]
    assert result.diagnostics[1].message == "missing ;"


def test_fake_tree_without_error_is_passed_and_has_error_without_nodes_is_failed(monkeypatch):
    import patch_verify

    _install_fake_parser(monkeypatch, _FakeNode("translation_unit", has_error=False))
    assert patch_verify.check_syntax("src/a.c", b"int x;").status == "passed"

    _install_fake_parser(monkeypatch, _FakeNode("translation_unit", has_error=True))
    result = patch_verify.check_syntax("src/a.c", b"int x;")
    assert result.status == "failed"
    assert [d.message for d in result.diagnostics] == ["syntax error (location unavailable)"]


@pytest.mark.parametrize("stage", ["loader", "constructor", "parse"])
def test_parser_infrastructure_failures_are_skipped_not_failed(runner: ToolExecutor, tmp_path: Path,
                                                              monkeypatch, spawn_guard, stage):
    import ast_parser
    import patch_verify

    class BrokenParser:
        def __init__(self, language):
            if stage == "constructor":
                raise RuntimeError("ABI mismatch")

        def parse(self, data):
            raise RuntimeError("parse crashed")

    if stage == "loader":
        monkeypatch.setattr(ast_parser, "_try_load_tree_sitter_language",
                            lambda lang: (_ for _ in ()).throw(RuntimeError("loader crashed")))
    else:
        monkeypatch.setattr(ast_parser, "_try_load_tree_sitter_language", lambda lang: object())
        monkeypatch.setattr(ast_parser, "Parser", BrokenParser)

    unit = patch_verify.check_syntax("src/a.c", b"int x;")
    assert unit.status == "skipped"
    assert unit.reason.startswith("parser unavailable (RuntimeError: ")

    out = _apply(runner, tmp_path, "main.c", "int x = 1;", "int x = 2;")
    assert (tmp_path / "main.c").read_text(encoding="utf-8") == "int x = 2;\n"
    lines = _verify_lines(out)
    assert lines[0] == "⚠ 驗證不完整（skipped: 1/1）——patch 已套用、未回滾"
    assert any(line.startswith("○ main.c: syntax skipped: parser unavailable (RuntimeError: ") for line in lines)


def test_verifier_exception_never_escapes_apply_patch(runner: ToolExecutor, tmp_path: Path,
                                                      monkeypatch, spawn_guard):
    import patch_verify

    monkeypatch.setattr(patch_verify, "verify_files",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("verifier exploded")))
    out = _apply(runner, tmp_path, "x.py", "x = 1", "x = 2")
    assert (tmp_path / "x.py").read_text(encoding="utf-8") == "x = 2\n"
    lines = _verify_lines(out)
    assert lines[0] == "⚠ 驗證不完整（skipped: 1/1）——patch 已套用、未回滾"
    assert "○ x.py: syntax skipped: verifier error (RuntimeError: verifier exploded)" in lines


FALLBACK_TITLE = "⚠ 驗證不完整（skipped: verifier error RuntimeError）——patch 已套用、未回滾"


@pytest.mark.parametrize("auto_verify", [False, True])
def test_renderer_exception_never_escapes_apply_patch(runner: ToolExecutor, tmp_path: Path,
                                                      monkeypatch, spawn_guard, auto_verify):
    """renderer 本身炸掉(停用/啟用兩條分支)也不得把例外拋回 apply_patch;fallback 文字硬編。"""
    import patch_verify

    monkeypatch.setattr(config, "PATCH_AUTO_VERIFY", auto_verify)
    monkeypatch.setattr(patch_verify, "render_report",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("renderer exploded")))
    out = _apply(runner, tmp_path, "x.py", "x = 1", "x = 2")
    assert (tmp_path / "x.py").read_text(encoding="utf-8") == "x = 2\n"
    lines = _verify_lines(out)
    assert lines[0] == FALLBACK_TITLE
    assert lines[-1].startswith(NEXT_STEPS)


# ---------------------------------------------------------------------------
# 讀檔只走 _safe_path + O_NOFOLLOW;patch_verify 自己永不開檔
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink containment")
def test_symlink_escape_target_is_skipped_without_reading(tmp_path: Path, monkeypatch, spawn_guard):
    import patch_verify

    monkeypatch.setattr(config, "PATCH_ENABLED", True)
    monkeypatch.setattr(config, "PATCH_AUTO_VERIFY", True)
    monkeypatch.setattr(config, "PATCH_VERIFY_STEPS", ["syntax"])
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n", encoding="utf-8")
    os.symlink(outside, sandbox / "escape.py")
    runner = ToolExecutor(str(sandbox))
    parsed: list = []
    monkeypatch.setattr(patch_verify, "check_syntax",
                        lambda rel, data: parsed.append(rel) or (_ for _ in ()).throw(AssertionError("read")))
    lines = runner._verify_patched_files(["escape.py"])
    assert lines[0] == "⚠ 驗證不完整（skipped: 1/1）——patch 已套用、未回滾"
    assert "○ escape.py: syntax skipped: path outside sandbox" in lines
    assert parsed == []


def test_identity_change_during_read_is_skipped(runner: ToolExecutor, tmp_path: Path,
                                                monkeypatch, spawn_guard):
    """實際呼叫順序必須是 fstat → read… → fstat;讀完後第二次 fstat 身分變了就拒絕採用。"""
    import types

    import patch_verify

    target = tmp_path / "x.py"
    target.write_text("x = 1\n", encoding="utf-8")
    target_ino = os.stat(target).st_ino
    real_fstat, real_read = os.fstat, os.read
    events: list[str] = []
    parsed: list = []

    def recording_fstat(fd):
        st = real_fstat(fd)
        if st.st_ino != target_ino:
            return st
        events.append("fstat")
        if events.count("fstat") == 2:
            return types.SimpleNamespace(st_mode=st.st_mode, st_ino=st.st_ino + 1, st_dev=st.st_dev)
        return st

    def recording_read(fd, size):
        data = real_read(fd, size)
        if real_fstat(fd).st_ino == target_ino:
            events.append("read")
        return data

    monkeypatch.setattr(os, "fstat", recording_fstat)
    monkeypatch.setattr(os, "read", recording_read)
    monkeypatch.setattr(patch_verify, "check_syntax",
                        lambda rel, data: parsed.append(rel) or (_ for _ in ()).throw(AssertionError("parsed")))
    lines = runner._verify_patched_files(["x.py"])
    assert events[0] == "fstat" and events[-1] == "fstat" and "read" in events, events
    assert events.count("fstat") == 2
    assert "○ x.py: syntax skipped: file identity changed during read" in lines
    assert lines[0] == "⚠ 驗證不完整（skipped: 1/1）——patch 已套用、未回滾"
    assert parsed == [], "身分變了的內容不得交給 parser"


def test_identity_stable_read_reaches_parser_with_two_fstats(runner: ToolExecutor, tmp_path: Path,
                                                           monkeypatch, spawn_guard):
    target = tmp_path / "x.py"
    target.write_text("x = 1\n", encoding="utf-8")
    target_ino = os.stat(target).st_ino
    real_fstat = os.fstat
    events: list[str] = []

    def recording_fstat(fd):
        st = real_fstat(fd)
        if st.st_ino == target_ino:
            events.append("fstat")
        return st

    monkeypatch.setattr(os, "fstat", recording_fstat)
    lines = runner._verify_patched_files(["x.py"])
    assert events == ["fstat", "fstat"]
    assert lines[0] == TITLE_PASSED


# ---------------------------------------------------------------------------
# renderer:diagnostics 上限、優先序
# ---------------------------------------------------------------------------
def test_diagnostics_are_capped_with_remainder_marker():
    import patch_verify

    diags = [patch_verify.Diagnostic("src/a.c", i + 1, f"syntax error near 'tok{i}'") for i in range(30)]
    result = patch_verify.StepResult("syntax", "src/a.c", "failed", diagnostics=diags)
    lines = patch_verify.render_report([result], requested=["syntax"], auto_verify=True)
    diag_lines = [line for line in lines if line.startswith("  src/a.c:")]
    marker = [line for line in lines if line.startswith("  …另 ")]
    assert len(diag_lines) == patch_verify.MAX_DIAGNOSTICS == 20
    assert marker == ["  …另 10 項未列出"]
    assert diag_lines == [f"  src/a.c:{i + 1}: syntax error near 'tok{i}'" for i in range(20)]

    long_diags = [patch_verify.Diagnostic("src/a.c", i + 1, "m" * 400) for i in range(10)]
    result = patch_verify.StepResult("syntax", "src/a.c", "failed", diagnostics=long_diags)
    lines = patch_verify.render_report([result], requested=["syntax"], auto_verify=True)
    diag_lines = [line for line in lines if line.startswith("  src/a.c:")]
    marker = [line for line in lines if line.startswith("  …另 ")]
    total = sum(len(line) + 1 for line in diag_lines + marker)
    assert total <= patch_verify.MAX_DIAGNOSTIC_CHARS == 1500
    assert 0 < len(diag_lines) < 10
    assert marker == [f"  …另 {10 - len(diag_lines)} 項未列出"]
    assert all(line.endswith("m" * 400) for line in diag_lines), "只在完整行邊界截斷"


def test_single_oversized_diagnostic_is_omitted_whole_not_truncated():
    import patch_verify

    huge = patch_verify.Diagnostic("src/a.c", 7, "x" * 1600)
    result = patch_verify.StepResult("syntax", "src/a.c", "failed", diagnostics=[huge])
    lines = patch_verify.render_report([result], requested=["syntax"], auto_verify=True)
    diag_lines = [line for line in lines if line.startswith("  src/a.c:")]
    marker = [line for line in lines if line.startswith("  …另 ")]
    assert diag_lines == [], "放不下的行整行省略,不得切斷內容"
    assert marker == ["  …另 1 項未列出"]
    assert sum(len(line) + 1 for line in diag_lines + marker) <= patch_verify.MAX_DIAGNOSTIC_CHARS
    assert "x" * 100 not in "\n".join(lines)


def test_more_than_collect_cap_error_nodes_are_counted_in_remainder(monkeypatch):
    import patch_verify

    root = _FakeNode("translation_unit", children=[
        _FakeNode("ERROR", is_error=True, row=i, text=b"bad") for i in range(250)
    ])
    _install_fake_parser(monkeypatch, root)
    result = patch_verify.check_syntax("src/a.c", b"...")
    assert result.status == "failed"
    assert len(result.diagnostics) == 200 and result.omitted_diagnostics == 50
    lines = patch_verify.render_report([result], requested=["syntax"], auto_verify=True)
    diag_lines = [line for line in lines if line.startswith("  src/a.c:")]
    assert len(diag_lines) == patch_verify.MAX_DIAGNOSTICS
    assert [line for line in lines if line.startswith("  …另 ")] == ["  …另 230 項未列出"]


def test_next_steps_hint_uses_public_tool_name_without_path_argument(runner: ToolExecutor,
                                                                     tmp_path: Path, monkeypatch,
                                                                     spawn_guard):
    """SEAMS S-D 固定文字:兩條渲染路徑(renderer 與 fallback)都寫 `codetrail_run_lint(fix=False)`,
    不得寫成 `codetrail_run_lint(path, fix=False)`。"""
    import patch_verify

    passed = patch_verify.StepResult("syntax", "x.py", "passed")
    rendered = [
        patch_verify.render_report([passed], requested=["syntax"], auto_verify=True)[-1],
        patch_verify.render_report([], requested=[], auto_verify=True)[-1],
        patch_verify.render_report([], requested=["syntax"], auto_verify=False)[-1],
    ]
    # 測試端固定的完整肯定句(不從 production 匯入):任何前綴否定(例如「請勿呼叫」)
    # 或改寫都會讓逐字比對失敗;舊的 `path` 形式另以否定斷言保留。
    expected_hint = (
        "建議下一步: 對改過的檔案呼叫 codetrail_run_lint(fix=False) 做 lint 檢查；"
        "codetrail_run_command(\"pytest ...\") 跑相關測試（各需獨立核准；apply_patch 不代跑）"
    )
    for hint in rendered:
        assert hint == expected_hint, hint
        assert "codetrail_run_lint(path" not in hint, hint

    (tmp_path / "x.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(patch_verify, "render_report",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("renderer exploded")))
    fallback = runner._verify_patched_files(["x.py"])
    assert fallback[0] == FALLBACK_TITLE
    assert fallback[-1] == expected_hint, fallback
    assert "codetrail_run_lint(path" not in fallback[-1], fallback


def test_render_precedence_failed_over_skipped_over_passed():
    import patch_verify

    passed = patch_verify.StepResult("syntax", "a.py", "passed")
    skipped = patch_verify.StepResult("syntax", "b.txt", "skipped", reason="parser unavailable for .txt")
    failed = patch_verify.StepResult("syntax", "c.py", "failed",
                                     diagnostics=[patch_verify.Diagnostic("c.py", 1, "invalid syntax")])
    assert patch_verify.render_report([passed, skipped, failed], requested=["syntax"], auto_verify=True)[0] == TITLE_FAILED
    assert patch_verify.render_report([passed, skipped], requested=["syntax"], auto_verify=True)[0] == \
        "⚠ 驗證不完整（skipped: 1/2）——patch 已套用、未回滾"
    assert patch_verify.render_report([passed], requested=["syntax"], auto_verify=True)[0] == TITLE_PASSED
    empty = patch_verify.render_report([], requested=[], auto_verify=True)
    assert empty[0] == "⚠ 驗證不完整（skipped: no verification step requested）——patch 已套用、未回滾"
    assert empty[-1].startswith(NEXT_STEPS)
    disabled = patch_verify.render_report([], requested=["syntax"], auto_verify=False)
    assert disabled[0] == TITLE_DISABLED
    assert disabled[-1].startswith(NEXT_STEPS)
