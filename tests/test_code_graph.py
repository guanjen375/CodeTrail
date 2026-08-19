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
            import fs_safety
            from code_graph import CodeGraph

            real_acquire = fs_safety.acquire_file_lock
            def announced_acquire(*args, **kwargs):
                print("LOCK_ATTEMPT", flush=True)
                return real_acquire(*args, **kwargs)
            fs_safety.acquire_file_lock = announced_acquire

            CodeGraph({str(tmp_path)!r}).build()
            print("BUILD_DONE")
        """)
        proc = subprocess.Popen([sys.executable, "-c", script],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        # 子行程在 blocking flock 前先送出握手；收到後即可確定 B 已走到
        # 取鎖邊界，不必用固定 1.5 秒猜排程是否跑到了。
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "LOCK_ATTEMPT"
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
            "INSERT INTO nodes VALUES "
            "('x','p.py','function','half','half',1,1,'t','exact','not_applicable',NULL)")
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
    with pytest.raises(CodeGraphError) as exc_info:
        fresh.ensure_fresh()
    message = str(exc_info.value)
    assert "損壞" in message
    assert "移出" in message or "刪除" in message
    assert str(fresh.db_file) in message
    assert fresh.build_command() in message
    with pytest.raises(CodeGraphError):
        fresh.find_nodes("entry")

    backup = fresh.db_file.with_suffix(".sqlite3.corrupt")
    fresh.db_file.replace(backup)
    fresh.build()
    assert fresh.find_nodes("entry")


def test_explicit_build_migrates_v1_graph_in_place(tmp_path):
    """The documented rebuild command must upgrade a pre-v2 DB without deletion."""
    _write_py_repo(tmp_path)
    graph = CodeGraph(str(tmp_path))
    conn = sqlite3.connect(graph.db_file)
    conn.executescript(
        """
        CREATE TABLE files(path TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
                           lang TEXT NOT NULL, backend TEXT NOT NULL);
        CREATE TABLE nodes(id TEXT PRIMARY KEY, path TEXT NOT NULL, kind TEXT NOT NULL,
                           name TEXT NOT NULL, qualified_name TEXT NOT NULL,
                           start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
                           backend TEXT NOT NULL, confidence TEXT NOT NULL);
        CREATE TABLE edges(src_kind TEXT NOT NULL, src_id TEXT NOT NULL, dst_kind TEXT,
                           dst_id TEXT, unresolved_target TEXT, ambiguity_group TEXT,
                           type TEXT NOT NULL, evidence_path TEXT NOT NULL,
                           evidence_line INTEGER NOT NULL, backend TEXT NOT NULL,
                           confidence TEXT NOT NULL);
        CREATE TABLE index_metadata(schema_version INTEGER NOT NULL,
                           scope_fingerprint TEXT NOT NULL,
                           parser_versions TEXT NOT NULL,
                           created TEXT NOT NULL, updated TEXT NOT NULL);
        INSERT INTO index_metadata VALUES (1, 'legacy', 'legacy', 'old', 'old');
        """
    )
    conn.close()

    graph.build()

    conn = sqlite3.connect(graph.db_file)
    try:
        assert conn.execute(
            "SELECT schema_version FROM index_metadata"
        ).fetchone() == (code_graph.GRAPH_SCHEMA_VERSION,)
        assert {row[1] for row in conn.execute("PRAGMA table_info(nodes)")} >= {
            "linkage", "condition"
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM declarations"
        ).fetchone()[0] >= 0
    finally:
        conn.close()
    assert graph.find_nodes("entry")


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
            "'calls','../../outside/secret.py',1,'python-ast','resolved',"
            "'global_unique',NULL)",
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


# ============================================================
# GPT 審核二輪修正的回歸測試(2026-08-19)
# ============================================================
def test_incremental_unique_to_ambiguous_updates_existing_caller(tmp_path):
    """審核二輪 #1:**修改既有檔**新增同名函式後(增量的 catalog delta 路徑,
    不是 added→full rebuild),未變的 caller 檔必須重 resolve 成歧義。"""
    (tmp_path / "lib_a.py").write_text(
        "def foo():\n    return 1\n", encoding="utf-8")
    (tmp_path / "lib_b.py").write_text(
        "def unrelated():\n    return 0\n", encoding="utf-8")
    (tmp_path / "caller.py").write_text(
        "def run():\n    return foo()\n", encoding="utf-8")
    g = CodeGraph(str(tmp_path))
    g.build()
    edges = g.callees("run")
    assert len(edges) == 1 and edges[0]["resolved"], "起點:唯一定義 → resolved"

    # 修改既有的 lib_b.py 加入同名定義;caller.py 本身完全沒變、無檔案增刪
    (tmp_path / "lib_b.py").write_text(
        "def unrelated():\n    return 0\n\n\ndef foo():\n    return 2\n",
        encoding="utf-8")
    code_rag.invalidate_scan_cache(tmp_path)
    g.ensure_fresh()

    edges = g.callees("run")
    assert len(edges) == 2, "同一 call site 應變成兩列歧義候選"
    assert all(e["resolved"] is False for e in edges), (
        "unique→ambiguous:舊的『確定呼叫』必須被重判成歧義")
    groups = {e["ambiguity_group"] for e in edges}
    assert len(groups) == 1 and None not in groups

    paths = g.shortest_evidence_paths({"run"}, {"foo"})
    assert paths == [], "歧義後不得再出現確定呼叫鏈"


def test_incremental_ambiguous_to_unique_updates_existing_caller(tmp_path):
    """審核二輪 #1 反向:刪掉一個同名定義後,歧義要收斂回 resolved。"""
    (tmp_path / "lib_a.py").write_text(
        "def foo():\n    return 1\n", encoding="utf-8")
    (tmp_path / "lib_b.py").write_text(
        "def foo():\n    return 2\n", encoding="utf-8")
    (tmp_path / "caller.py").write_text(
        "def run():\n    return foo()\n", encoding="utf-8")
    g = CodeGraph(str(tmp_path))
    g.build()
    assert all(not e["resolved"] for e in g.callees("run")), "起點:歧義"

    (tmp_path / "lib_b.py").unlink()
    code_rag.invalidate_scan_cache(tmp_path)
    g.ensure_fresh()

    edges = g.callees("run")
    assert len(edges) == 1, "歧義收斂後同一 call site 只剩一列"
    assert edges[0]["resolved"] is True
    assert edges[0]["dst_name"] == "foo"
    assert g.shortest_evidence_paths({"run"}, {"foo"}), "收斂後呼叫鏈恢復可用"


@pytest.mark.skipif(not HAS_TS_C, reason="tree-sitter-c 未安裝")
def test_python_body_only_edit_does_not_rebuild_for_c_name_collision(
    tmp_path, monkeypatch,
):
    """An unchanged callable catalog must not fan out to colliding C callers."""
    (tmp_path / "helpers.py").write_text(
        "def reset():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "caller.c").write_text(
        "int boot(void) { return reset(); }\n", encoding="utf-8"
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    (tmp_path / "helpers.py").write_text(
        "def reset():\n    return 2\n", encoding="utf-8"
    )
    code_rag.invalidate_scan_cache(tmp_path)

    def unexpected_full_rebuild(*args, **kwargs):
        raise AssertionError("body-only Python edit must remain incremental")

    monkeypatch.setattr(graph, "build", unexpected_full_rebuild)
    graph.ensure_fresh()

    [reset] = graph.find_nodes("reset")
    assert reset["path"] == "helpers.py"


def test_callable_multiplicity_change_still_reresolves_unchanged_callers(tmp_path):
    """Per-name node-id sets must detect a same-name overload addition."""
    (tmp_path / "library.py").write_text(
        "def dispatch():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "caller.py").write_text(
        "def run():\n    return dispatch()\n", encoding="utf-8"
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()
    assert [edge["resolved"] for edge in graph.callees("run")] == [True]

    (tmp_path / "library.py").write_text(
        "def dispatch():\n    return 1\n\n"
        "def dispatch(value):\n    return value\n",
        encoding="utf-8",
    )
    code_rag.invalidate_scan_cache(tmp_path)
    graph.ensure_fresh()

    edges = graph.callees("run")
    assert len(edges) == 2
    assert all(edge["resolved"] is False for edge in edges)


def test_missing_graph_raises_with_build_command(tmp_path):
    """審核二輪 #3:graph 未建立 → CodeGraphError(訊息含 CLI 建立命令),
    不做隱式 build、不留任何 graph 檔。"""
    _write_py_repo(tmp_path)
    g = CodeGraph(str(tmp_path))
    with pytest.raises(CodeGraphError, match=r"code_graph\.py --root"):
        g.ensure_fresh()
    assert not g.db_file.exists(), "fail-loud 路徑不得偷偷建檔"


def test_cli_builds_graph_and_is_idempotent(tmp_path):
    """顯式建圖入口:python code_graph.py --root <root>;重跑=in-place rebuild。"""
    _write_py_repo(tmp_path)
    script = REPO_ROOT / "code_graph.py"
    for _ in range(2):
        proc = subprocess.run(
            [sys.executable, str(script), "--root", str(tmp_path)],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        assert "done:" in proc.stdout
    g = CodeGraph(str(tmp_path))
    g.ensure_fresh()  # 建好之後不得再拋
    assert g.find_nodes("entry")


def test_parser_fingerprint_includes_grammar_distribution_versions():
    """審核二輪 #5:指紋要含三個 distribution 的實際版本,不是可載入布林。"""
    import importlib.metadata as _im

    g = CodeGraph.__new__(CodeGraph)
    fp = CodeGraph._parser_versions(g)
    for dist in ("tree-sitter", "tree-sitter-c", "tree-sitter-cpp"):
        try:
            version = _im.version(dist)
        except _im.PackageNotFoundError:
            version = "absent"
        assert version in fp, f"指紋缺 {dist} 版本({version}): {fp}"


# ============================================================
# GPT 審核三輪修正的回歸測試(2026-08-19)
# ============================================================
def test_added_file_switches_import_from_package_init_to_submodule(tmp_path):
    """三輪 #1(fallback→精確):`from pkg import util` 原落 pkg/__init__.py,
    新增 pkg/util.py 後 edge 必須切換(added → full rebuild)。"""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from pkg import util\n\n\ndef entry():\n    return util\n", encoding="utf-8")
    g = CodeGraph(str(tmp_path))
    g.build()
    assert g.file_includes("app.py") == ["pkg/__init__.py"], "起點:fallback 到 package init"

    (pkg / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    code_rag.invalidate_scan_cache(tmp_path)
    g.ensure_fresh()
    assert g.file_includes("app.py") == ["pkg/util.py"], (
        "新增子模組後 import edge 必須切換,不得留在 __init__.py")


@pytest.mark.skipif(not HAS_TS_C, reason="tree-sitter c 未安裝")
def test_added_same_name_header_turns_unique_include_into_ambiguity(tmp_path):
    """三輪 #1(唯一→歧義):新增第二個同名 header 後,既有 caller 的
    include 不得維持確定解析。"""
    inc_a = tmp_path / "inc_a"
    inc_a.mkdir()
    (inc_a / "config.h").write_text("#define A 1\n", encoding="utf-8")
    (tmp_path / "m.c").write_text(
        '#include "config.h"\n'
        "int use_cfg(void) {\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8")
    g = CodeGraph(str(tmp_path))
    g.build()
    assert g.file_includes("m.c") == ["inc_a/config.h"], "起點:唯一 resolve"

    inc_b = tmp_path / "inc_b"
    inc_b.mkdir()
    (inc_b / "config.h").write_text("#define B 1\n", encoding="utf-8")
    code_rag.invalidate_scan_cache(tmp_path)
    g.ensure_fresh()

    conn = sqlite3.connect(g.db_file)
    rows = conn.execute(
        "SELECT dst_id, ambiguity_group FROM edges WHERE src_id='m.c'"
        " AND type='includes'").fetchall()
    conn.close()
    assert len(rows) == 2, "同名兩候選:同一 include site 應產兩列"
    assert all(group is not None for _dst, group in rows), (
        "新增同名 header 後仍維持確定 include = 錯誤的確定性")


def test_added_file_resolves_previously_unresolved_call(tmp_path):
    """三輪 #1(未解析→已解析):新增定義檔後,既有 caller 的 unresolved
    call 必須 resolve。"""
    (tmp_path / "user.py").write_text(
        "def use_it():\n    return target_fn()\n", encoding="utf-8")
    g = CodeGraph(str(tmp_path))
    g.build()
    edges = g.callees("use_it")
    assert edges and not edges[0]["resolved"], "起點:unresolved"

    (tmp_path / "lib.py").write_text(
        "def target_fn():\n    return 1\n", encoding="utf-8")
    code_rag.invalidate_scan_cache(tmp_path)
    g.ensure_fresh()

    edges = g.callees("use_it")
    assert edges and edges[0]["resolved"], "新增定義檔後必須 resolve"
    assert edges[0]["dst_name"] == "target_fn"


def test_missing_graph_error_command_is_directly_executable(tmp_path):
    """三輪 #3:錯誤訊息裡的建圖命令必須「在任意 cwd 直接複製執行」就能建好
    (實際 interpreter + 絕對 script 路徑 + 實際 root,shell-quoted)。"""
    import re
    import shlex

    _write_py_repo(tmp_path)
    g = CodeGraph(str(tmp_path))
    with pytest.raises(CodeGraphError) as exc:
        g.ensure_fresh()
    m = re.search(r"`([^`]+)`", str(exc.value))
    assert m, f"錯誤訊息必須含反引號包住的命令: {exc.value}"
    cmd = shlex.split(m.group(1))
    assert cmd[0] == sys.executable, "必須是實際 interpreter,不是裸 python"
    assert Path(cmd[1]).is_absolute() and Path(cmd[1]).exists(), "script 必須是存在的絕對路徑"
    assert str(tmp_path) in cmd, "必須帶實際 root,不是 <AICODE_ROOT> placeholder"

    # 在「firmware repo」的 cwd(不是 CodeTrail repo)直接執行那條命令
    elsewhere = tmp_path / "somewhere_else"
    elsewhere.mkdir()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                          cwd=str(elsewhere))
    assert proc.returncode == 0, proc.stderr
    g.ensure_fresh()  # 建好後不得再拋
    assert g.find_nodes("entry")


def test_heuristic_attribute_call_never_enters_evidence_path(tmp_path):
    """`obj.target()` 只是「repo 內同名唯一」的猜測(confidence=heuristic)。

    它可以當線索,但 path mode 對外宣稱「只走確定解析的邊」,不能收它。
    """
    (tmp_path / "lib.py").write_text(
        "def target():\n    return 1\n", encoding="utf-8")
    (tmp_path / "caller.py").write_text(
        "def run(obj):\n"
        "    return obj.target()\n"
        "\n"
        "\n"
        "def run_direct():\n"
        "    return target()\n",
        encoding="utf-8",
    )
    g = CodeGraph(str(tmp_path))
    g.build()

    [attr_edge] = g.callees("run")
    assert attr_edge["resolved"] is True, "同名唯一仍給線索邊"
    assert attr_edge["confidence"] == "heuristic"
    assert g.shortest_evidence_paths({"run"}, {"target"}) == [], (
        "heuristic 邊不得出現在確定呼叫鏈")

    [name_edge] = g.callees("run_direct")
    assert name_edge["confidence"] in code_graph.CONFIRMED_EDGE_CONFIDENCE
    assert g.shortest_evidence_paths({"run_direct"}, {"target"}), (
        "確定解析的邊仍必須可走")
