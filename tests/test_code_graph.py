#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""code_graph 專屬測試(§7.7)+ stable ID(§7.2)。

雙 process 情境用 subprocess 跑真另一個 Python;WAL/flock 行為不能用
thread 模擬。tree-sitter c/cpp 缺席時 C 案例 skip(Python 案例照跑)。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ast_parser  # noqa: E402
import code_graph  # noqa: E402
import code_rag  # noqa: E402
import fs_safety  # noqa: E402
from code_graph import CodeGraph, CodeGraphError, make_node_id, normalize_signature  # noqa: E402

HAS_TS_C = bool(
    ast_parser.HAS_TREE_SITTER and ast_parser._try_load_tree_sitter_language("c")
)


@pytest.fixture(autouse=True)
def _fresh_scan_cache():
    code_rag._INDEX_SCAN_CACHE.clear()
    yield
    code_rag._INDEX_SCAN_CACHE.clear()


def _write_py_repo(root: Path) -> None:
    (root / "util.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8")
    (root / "app.py").write_text(
        "import util\n"
        "\n"
        "def entry():\n"
        "    return util.helper()\n",
        encoding="utf-8")


# ============================================================
# stable ID(§7.2;§6.3 的 overload golden 一併在此)
# ============================================================
def test_normalize_signature_strips_names_keeps_types():
    assert normalize_signature("int scale(int v)") == "int"
    assert normalize_signature("float scale(float v)") == "float"
    assert normalize_signature("void f(void)") == ""
    assert normalize_signature("int g(uint32_t *processed_out, const char *name)") == \
        "uint32_t*,constchar*"
    # 同 arity 不同型別必須不同
    assert normalize_signature("int f(int a)") != normalize_signature("int f(float a)")


def test_non_overload_signature_change_keeps_id_stable():
    sym_a = ast_parser.Symbol(
        name="f", type="function", start_line=1, end_line=3, context="",
        signature="int f(int a)", qualified_name="f")
    sym_b = ast_parser.Symbol(
        name="f", type="function", start_line=1, end_line=3, context="",
        signature="int f(int a, int b)", qualified_name="f")
    id_a = code_graph._assign_node_ids("m.c", "c", [sym_a])[0][0]
    id_b = code_graph._assign_node_ids("m.c", "c", [sym_b])[0][0]
    assert id_a == id_b, "非 overload 修改 signature 不得改變 ID"
    assert id_a == make_node_id("m.c", "c", "function", "f")


def test_overload_ids_differ_including_same_arity():
    def sym(sig, line):
        return ast_parser.Symbol(
            name="scale", type="function", start_line=line, end_line=line + 2,
            context="", signature=sig, qualified_name="scale")

    pairs = code_graph._assign_node_ids(
        "m.cpp", "cpp", [sym("int scale(int v)", 1), sym("float scale(float v)", 5)])
    ids = [nid for nid, _ in pairs]
    assert len(set(ids)) == 2, "同 arity 不同型別的 overload 必須得到不同 ID"


def test_identical_ifdef_twins_still_get_unique_ids():
    # variants_mini 場景:#ifdef 兩臂同名同 signature → occurrence tie-break
    def sym(line):
        return ast_parser.Symbol(
            name="v", type="function", start_line=line, end_line=line + 1,
            context="", signature="uint32_t v(void)", qualified_name="v")

    pairs = code_graph._assign_node_ids("m.c", "c", [sym(1), sym(6)])
    ids = [nid for nid, _ in pairs]
    assert len(set(ids)) == 2, "同款定義兩份也不得撞 PRIMARY KEY"


# ============================================================
# 建置 / 增量(Python репо,不依賴 tree-sitter)
# ============================================================
def test_build_and_python_imports_calls(tmp_path):
    _write_py_repo(tmp_path)
    g = CodeGraph(str(tmp_path))
    g.build()
    assert g.file_includes("app.py") == ["util.py"], "Python import → file edge"
    callees = g.callees("entry")
    assert any(e["dst_name"] == "helper" and e["resolved"] for e in callees)


def test_incremental_add_edit_delete_rename(tmp_path):
    _write_py_repo(tmp_path)
    g = CodeGraph(str(tmp_path))
    g.build()

    # add
    (tmp_path / "extra.py").write_text(
        "def added_fn():\n    return 9\n", encoding="utf-8")
    code_rag.invalidate_scan_cache(tmp_path)
    g.ensure_fresh()
    assert g.find_nodes("added_fn"), "新增檔案的符號要進 graph"

    # edit:helper 改名 → 舊節點消失,呼叫端(app.py 未變)的邊轉 unresolved
    (tmp_path / "util.py").write_text(
        "def helper_renamed():\n    return 1\n", encoding="utf-8")
    code_rag.invalidate_scan_cache(tmp_path)
    g.ensure_fresh()
    assert not g.find_nodes("helper")
    assert g.find_nodes("helper_renamed")
    callees = g.callees("entry")
    helper_edges = [e for e in callees if e["unresolved_target"] == "helper"]
    assert helper_edges and not helper_edges[0]["resolved"], (
        "指向已消失節點的 call 邊必須標 unresolved,不得懸空指舊 id")

    # delete
    (tmp_path / "extra.py").unlink()
    code_rag.invalidate_scan_cache(tmp_path)
    g.ensure_fresh()
    assert not g.find_nodes("added_fn")

    # rename(= delete + add)
    (tmp_path / "app.py").rename(tmp_path / "main_app.py")
    code_rag.invalidate_scan_cache(tmp_path)
    g.ensure_fresh()
    nodes = g.find_nodes("entry")
    assert nodes and nodes[0]["path"] == "main_app.py"


def test_scope_fingerprint_change_triggers_full_rebuild(tmp_path, capsys):
    _write_py_repo(tmp_path)
    g = CodeGraph(str(tmp_path))
    g.build()
    conn = sqlite3.connect(g.db_file)
    with conn:
        conn.execute("UPDATE index_metadata SET scope_fingerprint = 'stale'")
    conn.close()
    g.ensure_fresh()
    assert "full rebuild" in capsys.readouterr().err
    assert g.find_nodes("entry")


# ============================================================
# §7.7 雙 process:reader vs rebuild
# ============================================================
def _run_build_subprocess(root: Path, extra: str = "") -> subprocess.CompletedProcess:
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from code_graph import CodeGraph
        g = CodeGraph({str(root)!r})
        g.build()
        {extra}
        print("BUILD_DONE")
    """)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120)


def test_reader_survives_concurrent_rebuild(tmp_path):
    _write_py_repo(tmp_path)
    g = CodeGraph(str(tmp_path))
    g.build()

    # A:持讀 transaction(WAL snapshot)
    reader = sqlite3.connect(g.db_file)
    reader.execute("BEGIN")
    before = reader.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    assert before > 0

    # 改一個檔,讓 B 的 rebuild 產生可觀察的差異
    (tmp_path / "third.py").write_text("def third_fn():\n    return 3\n",
                                       encoding="utf-8")

    # B:另一個 process 整體 rebuild
    proc = _run_build_subprocess(tmp_path)
    assert proc.returncode == 0, proc.stderr

    # A 的舊 snapshot 不損毀、讀數一致
    still = reader.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    assert still == before, "WAL 下既有 reader 必須讀到舊 snapshot"
    reader.rollback()

    # A 的新查詢見新資料
    fresh = reader.execute(
        "SELECT COUNT(*) FROM nodes WHERE name='third_fn'").fetchone()[0]
    assert fresh == 1
    reader.close()

    check = sqlite3.connect(g.db_file)
    assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    check.close()


def test_second_process_must_not_delete_active_staging(tmp_path):
    """A 持鎖建 staging 中;B 拿不到鎖 → 不得清任何 .tmp*(§7.5/§7.7)。"""
    _write_py_repo(tmp_path)
    g = CodeGraph(str(tmp_path))

    # A(本 process)持鎖 + 佈一個 active staging 檔
    lock_fd = fs_safety.acquire_file_lock(g.lock_file, tmp_path)
    staging = tmp_path / f"{g.db_file.name}.tmp99999"
    staging.write_text("active staging of process A", encoding="utf-8")
    try:
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(REPO_ROOT)!r})
            from code_graph import CodeGraph
            CodeGraph({str(tmp_path)!r}).build()
            print("BUILD_DONE")
        """)
        proc = subprocess.Popen([sys.executable, "-c", script],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        # B 正在等鎖:staging 必須原封不動
        time.sleep(1.5)
        assert proc.poll() is None, "B 應該還在等 A 的鎖"
        assert staging.exists(), "B 沒拿到鎖就清了 active staging"
        assert staging.read_text(encoding="utf-8") == "active staging of process A"
    finally:
        fs_safety.release_file_lock(lock_fd)

    out, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, err
    assert "BUILD_DONE" in out
    # A 放鎖後 B 取鎖 → B 可以清殘留並完成建置
    assert not staging.exists(), "B 取鎖後應清掉殘留 staging"
    assert g.db_file.exists()
    assert g.find_nodes("entry")


def test_crash_mid_transaction_leaves_old_graph_intact(tmp_path):
    _write_py_repo(tmp_path)
    g = CodeGraph(str(tmp_path))
    g.build()
    before = {n["name"] for n in g.find_nodes("entry")}
    assert before

    # 另一 process:BEGIN IMMEDIATE + 全刪 + insert 一半,然後 hard kill
    script = textwrap.dedent(f"""
        import os, sqlite3, sys
        conn = sqlite3.connect({str(g.db_file)!r})
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM edges")
        conn.execute("DELETE FROM nodes")
        conn.execute(
            "INSERT INTO nodes VALUES ('x','p.py','function','half','half',1,1,'t','exact')")
        os._exit(1)
    """)
    proc = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1

    conn = sqlite3.connect(g.db_file)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    names = {r[0] for r in conn.execute("SELECT name FROM nodes")}
    conn.close()
    assert "half" not in names, "未 commit 的 transaction 必須完整 rollback"
    assert "entry" in names, "舊 graph 必須完整可讀"


def test_corrupt_db_gives_explicit_error(tmp_path):
    _write_py_repo(tmp_path)
    g = CodeGraph(str(tmp_path))
    g.build()
    g.db_file.write_bytes(b"garbage not a sqlite file")

    fresh = CodeGraph(str(tmp_path))
    with pytest.raises(CodeGraphError):
        fresh.ensure_fresh()
    with pytest.raises(CodeGraphError):
        fresh.find_nodes("entry")


def test_traversal_filters_out_of_scope_evidence(tmp_path):
    _write_py_repo(tmp_path)
    g = CodeGraph(str(tmp_path))
    g.build()
    conn = sqlite3.connect(g.db_file)
    entry_id = conn.execute(
        "SELECT id FROM nodes WHERE name='entry'").fetchone()[0]
    helper_id = conn.execute(
        "SELECT id FROM nodes WHERE name='helper'").fetchone()[0]
    with conn:
        conn.execute(
            "INSERT INTO edges VALUES ('symbol',?, 'symbol',?,NULL,NULL,"
            "'calls','../../outside/secret.py',1,'python-ast','resolved')",
            (entry_id, helper_id),
        )
    conn.close()

    for edge in g.callees("entry") + g.iter_call_edges():
        assert "outside" not in edge["evidence_path"], (
            "scope 外 evidence 必須在查詢端被濾掉")


def test_db_lock_symlink_is_rejected(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX symlink 防線")
    _write_py_repo(tmp_path)
    g = CodeGraph(str(tmp_path))
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    os.symlink(victim, g.lock_file)
    with pytest.raises(fs_safety.FsSafetyError):
        g.build()
    assert victim.read_text(encoding="utf-8") == "keep"


# ============================================================
# C fixture(tree-sitter 在場才跑)
# ============================================================
@pytest.mark.skipif(not HAS_TS_C, reason="tree-sitter c 未安裝")
def test_c_includes_and_function_pointer_unresolved(tmp_path):
    (tmp_path / "q.h").write_text(
        "#ifndef Q_H\n#define Q_H\nint q_pop(void);\n#endif\n", encoding="utf-8")
    (tmp_path / "q.c").write_text(
        '#include "q.h"\n'
        "int q_pop(void) {\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8")
    (tmp_path / "d.c").write_text(
        '#include "q.h"\n'
        "typedef void (*cb_t)(void);\n"
        "static cb_t stored_cb;\n"
        "void drive(cb_t cb) {\n"
        "    q_pop();\n"
        "    cb();\n"
        "}\n",
        encoding="utf-8")
    g = CodeGraph(str(tmp_path))
    g.build()
    assert g.file_includes("d.c") == ["q.h"]
    callees = g.callees("drive")
    resolved = {e["dst_name"] for e in callees if e["resolved"]}
    unresolved = {e["unresolved_target"] for e in callees if not e["resolved"]}
    assert resolved == {"q_pop"}
    assert "cb" in unresolved, "function pointer 呼叫必須 unresolved,不得錯誤 resolve"


@pytest.mark.skipif(not HAS_TS_C, reason="tree-sitter c 未安裝")
def test_ambiguous_call_produces_ambiguity_group(tmp_path):
    (tmp_path / "a.c").write_text(
        "int shared_impl(void) {\n    return 1;\n}\n", encoding="utf-8")
    (tmp_path / "b.c").write_text(
        "int shared_impl(void) {\n    return 2;\n}\n", encoding="utf-8")
    (tmp_path / "m.c").write_text(
        "int shared_impl(void);\n"
        "int main_entry(void) {\n"
        "    return shared_impl();\n"
        "}\n",
        encoding="utf-8")
    g = CodeGraph(str(tmp_path))
    g.build()
    edges = [e for e in g.callees("main_entry") if e["unresolved_target"] == "shared_impl"]
    assert len(edges) == 2, "同名多候選:同一 call site 產多列"
    groups = {e["ambiguity_group"] for e in edges}
    assert len(groups) == 1 and None not in groups, "多列必須共用 ambiguity_group"
    assert all(e["confidence"] == "syntactic" for e in edges)


# ============================================================
# GPT 審核修正的回歸測試(2026-08-19 二輪)
# ============================================================
def test_incremental_caller_edit_keeps_cross_file_calls_resolved(tmp_path):
    """審核 #1:只改 caller 檔時,指向未修改檔案的 call 不得變 unresolved。"""
    _write_py_repo(tmp_path)
    g = CodeGraph(str(tmp_path))
    g.build()

    # 只改 app.py(caller);util.py(callee)完全不動
    (tmp_path / "app.py").write_text(
        "import util\n"
        "\n"
        "def entry():\n"
        "    x = 1\n"
        "    return util.helper()\n",
        encoding="utf-8")
    code_rag.invalidate_scan_cache(tmp_path)
    g.ensure_fresh()

    callees = g.callees("entry")
    helper_edges = [e for e in callees if e["dst_name"] == "helper"]
    assert helper_edges and helper_edges[0]["resolved"], (
        "增量重抽 caller 後,跨檔 call 必須仍 resolve 到未變檔案的節點")


def test_incremental_deleted_callee_keeps_real_name_not_question_mark(tmp_path):
    """審核 #1:刪 callee 檔(caller 非 reverse dep)→ unresolved_target 是真名。

    user.py 對 lib.py 沒有 import(bare-name call),所以刪 lib.py 時 user.py
    不會被重抽 —— 走的是 transaction 內的 dangling UPDATE 路徑。
    """
    (tmp_path / "lib.py").write_text(
        "def target_fn():\n    return 1\n", encoding="utf-8")
    (tmp_path / "user.py").write_text(
        "def use_it():\n    return target_fn()\n", encoding="utf-8")
    g = CodeGraph(str(tmp_path))
    g.build()
    assert any(e["resolved"] for e in g.callees("use_it"))

    (tmp_path / "lib.py").unlink()
    code_rag.invalidate_scan_cache(tmp_path)
    g.ensure_fresh()

    edges = g.callees("use_it")
    assert edges, "邊必須還在(轉 unresolved),不是消失"
    assert edges[0]["resolved"] is False
    assert edges[0]["unresolved_target"] == "target_fn", (
        f"dangling 名稱必須在 DELETE 前蒐集,不得退化成 '?';得到 {edges[0]['unresolved_target']!r}")


def test_db_symlink_is_rejected_on_query_and_refresh(tmp_path):
    """審核 #2:一般查詢與增量的 connect 也要過 symlink 防線,不只 build()。"""
    if os.name == "nt":
        pytest.skip("POSIX symlink 防線")
    _write_py_repo(tmp_path)
    g = CodeGraph(str(tmp_path))
    g.build()

    other = tmp_path / "other-project.sqlite3"
    other.write_bytes(g.db_file.read_bytes())
    g.db_file.unlink()
    os.symlink(other, g.db_file)

    fresh = CodeGraph(str(tmp_path))
    with pytest.raises(fs_safety.FsSafetyError):
        fresh.find_nodes("entry")
    with pytest.raises(fs_safety.FsSafetyError):
        fresh.ensure_fresh()


@pytest.mark.skipif(not HAS_TS_C, reason="tree-sitter c 未安裝")
def test_ambiguous_edges_are_not_resolved_and_never_enter_paths(tmp_path):
    """審核 #3:歧義候選 resolved=False;最短路徑不得走歧義邊。"""
    (tmp_path / "a.c").write_text(
        "int shared_impl(void) {\n    return 1;\n}\n", encoding="utf-8")
    (tmp_path / "b.c").write_text(
        "int shared_impl(void) {\n    return 2;\n}\n", encoding="utf-8")
    (tmp_path / "m.c").write_text(
        "int shared_impl(void);\n"
        "int main_entry(void) {\n"
        "    return shared_impl();\n"
        "}\n",
        encoding="utf-8")
    g = CodeGraph(str(tmp_path))
    g.build()

    edges = [e for e in g.callees("main_entry") if e["ambiguity_group"]]
    assert edges, "歧義邊要存在(資訊性)"
    assert all(e["resolved"] is False for e in edges), (
        "歧義候選有 dst_id 但只是候選之一,resolved 必須是 False")

    paths = g.shortest_evidence_paths({"main_entry"}, {"shared_impl"})
    assert paths == [], "呼叫鏈不得把歧義候選之一呈現成確定路徑"


def test_traversal_uses_single_connection_snapshot(tmp_path, monkeypatch):
    """審核 #4:一次 traversal 的所有查詢共用一條連線(WAL snapshot 一致)。

    連線數固定 = _require_ready 一條 + read snapshot 一條;若隨 BFS 節點數
    增長就是回歸(每查詢一條連線 = 可能混兩個 graph 世代)。
    """
    _write_py_repo(tmp_path)
    (tmp_path / "third.py").write_text(
        "import util\n\n\ndef third_fn():\n    return util.helper()\n",
        encoding="utf-8")
    g = CodeGraph(str(tmp_path))
    g.build()

    counts = {"n": 0}
    real_connect = code_graph.sqlite3.connect

    def counting_connect(*args, **kwargs):
        counts["n"] += 1
        return real_connect(*args, **kwargs)

    helper_id = g.find_nodes("helper")[0]["id"]
    monkeypatch.setattr(code_graph.sqlite3, "connect", counting_connect)

    for call in (
        lambda: g.neighbors(helper_id, hops=2),
        lambda: g.shortest_evidence_paths({"entry"}, {"helper"}),
        lambda: g.callees("entry"),
        lambda: g.callers("helper"),
        lambda: g.iter_call_edges(),
        lambda: g.relations_for_symbol("helper"),
        lambda: g.file_neighbors("app.py"),
    ):
        counts["n"] = 0
        call()
        assert counts["n"] <= 3, (
            f"traversal 開了 {counts['n']} 條連線;必須是固定小常數"
            "(_require_ready + snapshot),不得隨節點數增長")


def test_parser_capability_change_triggers_full_rebuild(tmp_path, capsys):
    """審核 #6:parser_versions 進 freshness;grammar/裝置狀態變了要 full rebuild。"""
    _write_py_repo(tmp_path)
    g = CodeGraph(str(tmp_path))
    g.build()

    conn = sqlite3.connect(g.db_file)
    with conn:
        conn.execute("UPDATE index_metadata SET parser_versions = 'stale-parsers'")
    conn.close()

    g.ensure_fresh()
    assert "parser capabilities changed" in capsys.readouterr().err
    conn = sqlite3.connect(g.db_file)
    stored = conn.execute("SELECT parser_versions FROM index_metadata").fetchone()[0]
    conn.close()
    assert stored == g._parser_versions(), "rebuild 後 parser_versions 必須更新"


def test_from_import_prefers_submodule_over_package_init(tmp_path):
    """審核 #7:`from pkg import util` 要優先連 pkg/util.py,不是 pkg/__init__.py。"""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from pkg import util\n"
        "\n"
        "def entry():\n"
        "    return util.helper()\n",
        encoding="utf-8")
    g = CodeGraph(str(tmp_path))
    g.build()
    assert g.file_includes("app.py") == ["pkg/util.py"], (
        "alias 接上 module path 後必須先解析子模組")


def test_relative_from_import_alias_resolves(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (pkg / "app.py").write_text(
        "from . import util\n"
        "\n"
        "def entry():\n"
        "    return util.helper()\n",
        encoding="utf-8")
    g = CodeGraph(str(tmp_path))
    g.build()
    assert g.file_includes("pkg/app.py") == ["pkg/util.py"]
