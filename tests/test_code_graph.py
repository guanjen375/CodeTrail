#!/usr/bin/env python3
"""tree-sitter C/C++ 解析、code graph 建置、C/C++ 可見性、定義 metadata 傳播。

四個主題原本各自一檔,合併於此(各區段前有 `# ── 原 …` 分隔註解):

* code_graph 專屬測試(§7.7)+ stable ID(§7.2)。雙 process 情境用 subprocess 跑真
  另一個 Python;WAL/flock 行為不能用 thread 模擬。tree-sitter c/cpp 缺席時 C 案例
  skip(Python 案例照跑)。
* C/C++ 保守解析(visible declaration、條件式 include)與增量 vs full rebuild 等價。
* C/C++ tree-sitter parser golden tests(§6.3)。需要 tree-sitter + tree-sitter-c +
  tree-sitter-cpp(requirements.txt 已釘版);沒裝時這批案例 skip —— 但 doctor /
  build_index 會把 degraded 顯式標出來,不是安靜跳過。
* definition metadata 的全鏈路保存(施工規格 §6 P2-4)。NEW SILENT CONTRACT。
  這條鏈的每一段斷掉都是無聲的:

  - parser 算出 linkage / condition / storage_class,但 ``_extract_symbols``
    沒複製 → symbol dict 就掉了。
  - symbol dict 有,但 ``_index_single_file`` 沒寫進 index entry → 持久 cache 掉了。
  - index entry 有,但 renderer 不吃 → 檢索看不到。
  - parser 都算好了,但 ``graph_linkage()`` 對非 callable 一律回
    ``not_applicable`` → graph node 又丟一次。

  「parser 支援了」不等於「retrieval / graph 保留了」——這些測試守的就是那個落差。
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
from ast_parser import get_parser_status, parse_file  # noqa: E402
from code_graph import CodeGraph, CodeGraphError, make_node_id, normalize_signature  # noqa: E402

HAS_TS_C = bool(
    ast_parser.HAS_TREE_SITTER and ast_parser._try_load_tree_sitter_language("c")
)

# 原 test_code_graph_cpp_visibility.py 與 test_ast_parser_cpp.py 各有一個 module 層
# skipif(條件相同、reason 字串不同)。合併後這個檔也含不依賴 tree-sitter 的
# Python 案例,所以改成逐條套用;兩個 reason 各自保留。
_MISSING_TS_C_CPP = (
    not ast_parser.HAS_TREE_SITTER
    or not ast_parser._try_load_tree_sitter_language("c")
    or not ast_parser._try_load_tree_sitter_language("cpp")
)
requires_ts_c_cpp = pytest.mark.skipif(
    _MISSING_TS_C_CPP, reason="tree-sitter c/cpp 未安裝")
requires_ts_c_cpp_pinned = pytest.mark.skipif(
    _MISSING_TS_C_CPP, reason="tree-sitter c/cpp 未安裝(requirements.txt 有釘版)")

# 各來源原本各自帶一份 autouse `_fresh_scan_cache`(清 code_rag._INDEX_SCAN_CACHE);
# 現在由 tests/conftest.py 的全域 autouse `_isolate_code_rag_scan_cache` 統一負責。


def _write_py_repo(root: Path) -> None:
    (root / "util.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8")
    (root / "app.py").write_text(
        "import util\n"
        "\n"
        "def entry():\n"
        "    return util.helper()\n",
        encoding="utf-8")


def _write(root: Path, rel_path: str, content: str) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ── 原 test_code_graph.py:code_graph 建置 / 增量 / 雙 process / stable ID(§7.2、§7.7)──
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


@pytest.mark.smoke
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


# ── 原 test_code_graph_cpp_visibility.py:C/C++ 保守解析、條件式 include、增量 vs full rebuild 等價 ──
def _source_edges(graph: CodeGraph, path: str, symbol: str) -> list[dict]:
    [node] = [n for n in graph.find_nodes(symbol) if n["path"] == path]
    return [
        edge
        for edge in graph.neighbors(
            node["id"], edge_types=("calls",), direction="out", hops=1, limit=100
        )["edges"]
        if edge["src_id"] == node["id"]
    ]


@requires_ts_c_cpp
def test_visible_declaration_uses_transitive_and_repo_angle_include(tmp_path):
    _write(
        tmp_path,
        "include/fw/service.h",
        "#ifndef FW_SERVICE_H\n"
        "#define FW_SERVICE_H\n"
        "int service_commit(unsigned generation);\n"
        "#endif\n",
    )
    _write(
        tmp_path,
        "include/fw/facade.h",
        "#ifndef FW_FACADE_H\n"
        "#define FW_FACADE_H\n"
        '#include "fw/service.h"\n'
        "#endif\n",
    )
    _write(
        tmp_path,
        "src/service.c",
        '#include "fw/service.h"\n'
        "int service_commit(unsigned generation) { return (int)generation; }\n",
    )
    _write(
        tmp_path,
        "src/direct_user.c",
        "#include <fw/service.h>\n"
        "int direct_flush(void) { return service_commit(7u); }\n",
    )
    _write(
        tmp_path,
        "src/transitive_user.c",
        '#include "fw/facade.h"\n'
        "int transitive_flush(void) { return service_commit(8u); }\n",
    )

    graph = CodeGraph(str(tmp_path))
    graph.build()

    assert graph.file_includes("src/direct_user.c") == ["include/fw/service.h"]
    for path, symbol in (
        ("src/direct_user.c", "direct_flush"),
        ("src/transitive_user.c", "transitive_flush"),
    ):
        [edge] = _source_edges(graph, path, symbol)
        assert edge["resolved"] is True
        assert edge["dst_name"] == "service_commit"
        assert edge["resolution_basis"] == "visible_declaration"

    [definition] = [
        node for node in graph.find_nodes("service_commit")
        if node["path"] == "src/service.c"
    ]
    assert definition["linkage"] == "external"

    conn = sqlite3.connect(graph.db_file)
    declaration = conn.execute(
        "SELECT linkage, condition FROM declarations"
        " WHERE path='include/fw/service.h' AND name='service_commit'"
    ).fetchone()
    angle_basis = conn.execute(
        "SELECT resolution_basis FROM edges"
        " WHERE src_id='src/direct_user.c' AND type='includes'"
    ).fetchone()
    conn.close()
    assert declaration == ("external", "#ifndef FW_SERVICE_H")
    assert angle_basis == ("unique_repo_angle_include",)


@requires_ts_c_cpp
def test_static_condition_macro_and_callback_stay_conservative(tmp_path):
    _write(
        tmp_path,
        "src/a.c",
        "static int init(void) { return 1; }\n"
        "int boot_a(void) { return init(); }\n",
    )
    _write(
        tmp_path,
        "src/b.c",
        "static int init(void) { return 2; }\n"
        "int boot_b(void) { return init(); }\n",
    )
    _write(tmp_path, "src/rogue.c", "int rogue(void) { return init(); }\n")
    _write(
        tmp_path,
        "src/variant.c",
        "#if defined(BOARD_ALPHA)\n"
        "int variant_init(void) { return 1; }\n"
        "#else\n"
        "int variant_init(void) { return 2; }\n"
        "#endif\n"
        "int variant_boot(void) { return variant_init(); }\n",
    )
    _write(
        tmp_path,
        "src/indirect.c",
        "typedef int (*callback_t)(void);\n"
        "#define RUN_CALLBACK(cb) cb()\n"
        "int callback_entry(callback_t callback) { return callback(); }\n"
        "int macro_entry(callback_t callback) { return RUN_CALLBACK(callback); }\n",
    )
    # Deliberate lexical decoys: repo-global uniqueness must not create a C call edge.
    _write(
        tmp_path,
        "src/decoys.c",
        "int callback(void) { return 9; }\n"
        "int RUN_CALLBACK(callback_t callback) { return callback(); }\n",
    )

    graph = CodeGraph(str(tmp_path))
    graph.build()

    for path, source in (("src/a.c", "boot_a"), ("src/b.c", "boot_b")):
        [edge] = _source_edges(graph, path, source)
        assert edge["resolved"] is True
        assert edge["dst_name"] == "init"
        assert edge["resolution_basis"] == "same_file"

    [rogue] = _source_edges(graph, "src/rogue.c", "rogue")
    assert rogue["resolved"] is False
    assert rogue["unresolved_target"] == "init"
    assert rogue["resolution_basis"] == "syntactic_only"

    variant_edges = _source_edges(graph, "src/variant.c", "variant_boot")
    assert len(variant_edges) == 2
    assert all(not edge["resolved"] for edge in variant_edges)
    assert {edge["resolution_basis"] for edge in variant_edges} == {
        "ambiguous_condition"
    }
    assert len({edge["ambiguity_group"] for edge in variant_edges}) == 1
    assert graph.shortest_evidence_paths({"variant_boot"}, {"variant_init"}) == []
    assert len({
        node["condition"] for node in graph.find_nodes("variant_init")
    }) == 2

    [callback] = _source_edges(graph, "src/indirect.c", "callback_entry")
    [macro] = _source_edges(graph, "src/indirect.c", "macro_entry")
    assert (callback["resolved"], callback["unresolved_target"]) == (False, "callback")
    assert (macro["resolved"], macro["unresolved_target"]) == (False, "RUN_CALLBACK")


@requires_ts_c_cpp
def test_cpp_exact_qualified_name_is_a_conservative_fallback(tmp_path):
    _write(
        tmp_path,
        "impl.cpp",
        "namespace service { int commit(void) { return 1; } }\n",
    )
    _write(
        tmp_path,
        "user.cpp",
        "int qualified_user(void) { return service::commit(); }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "user.cpp", "qualified_user")
    assert edge["resolved"] is True
    assert edge["dst_name"] == "commit"
    assert edge["resolution_basis"] == "qualified"


@requires_ts_c_cpp
def test_cpp_global_qualified_call_resolves_only_the_global_definition(tmp_path):
    _write(tmp_path, "impl.cpp", "int helper(void) { return 1; }\n")
    _write(
        tmp_path,
        "user.cpp",
        "namespace decoy { int helper(void) { return 2; } }\n"
        "int run(void) { return ::helper(); }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "user.cpp", "run")
    assert edge["resolved"] is True
    assert edge["dst_id"] in {
        node["id"] for node in graph.find_nodes("helper")
        if node["path"] == "impl.cpp"
    }
    assert edge["resolution_basis"] == "qualified"


@requires_ts_c_cpp
def test_cpp_unconditional_qualified_overloads_are_qualified_ambiguity(tmp_path):
    _write(
        tmp_path,
        "impl.cpp",
        "namespace service {\n"
        "int helper(int value) { return value; }\n"
        "int helper(double value) { return (int)value; }\n"
        "}\n",
    )
    _write(
        tmp_path,
        "user.cpp",
        "int run(void) { return service::helper(1); }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    edges = _source_edges(graph, "user.cpp", "run")
    assert len(edges) == 2
    assert all(edge["resolved"] is False for edge in edges)
    assert {edge["resolution_basis"] for edge in edges} == {
        "ambiguous_qualified"
    }
    assert len({edge["ambiguity_group"] for edge in edges}) == 1


@requires_ts_c_cpp
def test_cpp_bare_call_does_not_resolve_to_unrelated_method(tmp_path):
    _write(
        tmp_path,
        "same.cpp",
        "class Device { public: static int reset(void) { return 1; } };\n"
        "int boot(void) { return reset(); }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "same.cpp", "boot")
    assert edge["resolved"] is False
    assert edge["unresolved_target"] == "reset"


@requires_ts_c_cpp
def test_cpp_bare_call_within_same_class_still_resolves_method(tmp_path):
    _write(
        tmp_path,
        "same.cpp",
        "class Device { public:\n"
        "  static int reset(void) { return 1; }\n"
        "  static int boot(void) { return reset(); }\n"
        "};\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "same.cpp", "boot")
    assert edge["resolved"] is True
    assert edge["dst_name"] == "reset"
    assert edge["resolution_basis"] == "same_file"


@requires_ts_c_cpp
def test_cpp_method_can_resolve_visible_global_function(tmp_path):
    _write(tmp_path, "api.h", "int global_reset(void);\n")
    _write(tmp_path, "impl.cpp", "int global_reset(void) { return 1; }\n")
    _write(
        tmp_path,
        "user.cpp",
        '#include "api.h"\n'
        "class Device { public: static int boot(void) { return global_reset(); } };\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "user.cpp", "boot")
    assert edge["resolved"] is True
    assert edge["dst_name"] == "global_reset"
    assert edge["resolution_basis"] == "visible_declaration"


@requires_ts_c_cpp
def test_cpp_qualified_call_is_not_consumed_by_bare_declaration(tmp_path):
    _write(tmp_path, "api.h", "int commit(void);\n")
    _write(tmp_path, "impl.cpp", "int commit(void) { return 1; }\n")
    _write(
        tmp_path,
        "user.cpp",
        '#include "api.h"\nint run(void) { return service::commit(); }\n',
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "user.cpp", "run")
    assert edge["resolved"] is False
    assert edge["unresolved_target"] == "service::commit"


@requires_ts_c_cpp
def test_bare_angle_include_does_not_resolve_to_vendored_shim(tmp_path):
    _write(tmp_path, "include/stdint.h", "typedef unsigned fake_uint32_t;\n")
    _write(tmp_path, "src/user.c", "#include <stdint.h>\nint run(void) { return 0; }\n")
    graph = CodeGraph(str(tmp_path))
    graph.build()

    assert graph.file_includes("src/user.c") == []
    rows = graph.file_neighbors("src/user.c", limit=20)["edges"]
    assert not any(edge["resolved"] for edge in rows)


@requires_ts_c_cpp
def test_bare_angle_include_with_multiple_repo_candidates_stays_explicit(tmp_path):
    _write(tmp_path, "board_a/platform.h", "int board_a(void);\n")
    _write(tmp_path, "board_b/platform.h", "int board_b(void);\n")
    _write(tmp_path, "src/user.c", "#include <platform.h>\nint run(void) { return 0; }\n")
    graph = CodeGraph(str(tmp_path))
    graph.build()

    include_edges = [
        edge for edge in graph.file_neighbors("src/user.c", limit=20)["edges"]
        if edge["type"] == "includes"
    ]
    assert len(include_edges) == 2
    assert {edge["dst_id"] for edge in include_edges} == {
        "board_a/platform.h", "board_b/platform.h"
    }
    assert {edge["resolution_basis"] for edge in include_edges} == {
        "ambiguous_include"
    }
    assert all(edge["resolved"] is False for edge in include_edges)
    assert len({edge["ambiguity_group"] for edge in include_edges}) == 1


@requires_ts_c_cpp
def test_absolute_angle_include_cannot_suffix_match_a_vendored_header(tmp_path):
    _write(tmp_path, "repo_headers/usr/include/platform.h", "int fake_platform(void);\n")
    _write(
        tmp_path,
        "src/user.c",
        "#include </usr/include/platform.h>\nint run(void) { return 0; }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    assert graph.file_includes("src/user.c") == []
    assert not [
        edge for edge in graph.file_neighbors("src/user.c", limit=20)["edges"]
        if edge["type"] == "includes"
    ]


@requires_ts_c_cpp
def test_single_conditional_definition_remains_an_explicit_candidate(tmp_path):
    _write(
        tmp_path,
        "src/conditional.c",
        "#ifdef FEATURE_X\n"
        "int optional_impl(void) { return 1; }\n"
        "#endif\n"
        "int run(void) { return optional_impl(); }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/conditional.c", "run")
    assert edge["resolved"] is False
    assert edge["dst_name"] == "optional_impl"
    assert edge["ambiguity_group"] is not None
    assert edge["resolution_basis"] == "conditional_candidate"


@requires_ts_c_cpp
def test_mutually_exclusive_same_file_definition_does_not_hide_external_target(
    tmp_path,
):
    _write(tmp_path, "include/api.h", "int helper(void);\n")
    _write(tmp_path, "src/impl.c", "int helper(void) { return 7; }\n")
    _write(
        tmp_path,
        "src/user.c",
        "#ifdef USE_LOCAL_HELPER\n"
        "static int helper(void) { return 1; }\n"
        "#else\n"
        '#include "api.h"\n'
        "int run(void) { return helper(); }\n"
        "#endif\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/user.c", "run")
    assert edge["resolved"] is True
    assert edge["dst_id"] in {
        node["id"] for node in graph.find_nodes("helper")
        if node["path"] == "src/impl.c"
    }
    assert edge["resolution_basis"] == "visible_declaration"


@requires_ts_c_cpp
def test_weak_default_block_is_not_treated_as_include_guard(tmp_path):
    _write(
        tmp_path,
        "src/defaults.c",
        "#ifndef HAVE_PLATFORM_IMPL\n"
        "#define HAVE_PLATFORM_IMPL\n"
        "int platform_impl(void) { return 1; }\n"
        "#endif\n"
        "int run(void) { return platform_impl(); }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/defaults.c", "run")
    assert edge["resolved"] is False
    assert edge["resolution_basis"] == "conditional_candidate"


@requires_ts_c_cpp
def test_header_weak_default_block_is_not_treated_as_include_guard(tmp_path):
    _write(
        tmp_path,
        "include/defaults.h",
        "#ifndef HAVE_PLATFORM_IMPL\n"
        "#define HAVE_PLATFORM_IMPL\n"
        "int platform_impl(void);\n"
        "#endif\n",
    )
    _write(
        tmp_path,
        "src/platform.c",
        "int platform_impl(void) { return 1; }\n",
    )
    _write(
        tmp_path,
        "src/user.c",
        '#include "defaults.h"\n'
        "int run(void) { return platform_impl(); }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/user.c", "run")
    assert edge["resolved"] is False
    assert edge["unresolved_target"] == "platform_impl"


@requires_ts_c_cpp
def test_out_of_class_declaration_identity_matches_ast_definition(tmp_path):
    _write(
        tmp_path,
        "api.hpp",
        "namespace service {\n"
        "class Device { public: static int helper(); };\n"
        "int Device::helper();\n"
        "}\n",
    )
    _write(
        tmp_path,
        "impl.cpp",
        "namespace service {\n"
        "class Device;\n"
        "int Device::helper() { return 1; }\n"
        "}\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    conn = sqlite3.connect(graph.db_file)
    try:
        declaration = conn.execute(
            "SELECT qualified_name FROM declarations"
            " WHERE path='api.hpp' AND name='helper'"
        ).fetchone()
        definition = conn.execute(
            "SELECT qualified_name FROM nodes"
            " WHERE path='impl.cpp' AND name='Device::helper'"
        ).fetchone()
    finally:
        conn.close()
    assert declaration == definition == ("service::Device::helper",)


@requires_ts_c_cpp
@pytest.mark.parametrize(
    ("header", "symbol", "body"),
    [
        (
            "include/pragma_api.h",
            "pragma_api",
            "#pragma once\n"
            "#ifndef PRAGMA_API_H\n#define PRAGMA_API_H\n"
            "int pragma_api(void);\n#endif\n",
        ),
        (
            "include/wrapped_api.h",
            "wrapped_api",
            "extern int header_prefix;\n"
            "#ifndef WRAPPED_API_H\n#define WRAPPED_API_H\n"
            "int wrapped_api(void);\n#endif\n"
            "extern int header_suffix;\n",
        ),
        (
            "include/string_api.h",
            "string_api",
            'static const char *comment_token = "/*";\n'
            "#ifndef STRING_API_H\n#define STRING_API_H\n"
            "int string_api(void);\n#endif\n"
            "/* a real trailing comment */\n",
        ),
    ],
)
def test_common_include_guard_wrappers_remain_visibility_neutral(
    tmp_path, header, symbol, body,
):
    _write(tmp_path, header, body)
    _write(tmp_path, f"src/{symbol}.c", f"int {symbol}(void) {{ return 1; }}\n")
    _write(
        tmp_path,
        f"src/use_{symbol}.c",
        f'#include "{Path(header).name}"\n'
        f"int use_{symbol}(void) {{ return {symbol}(); }}\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, f"src/use_{symbol}.c", f"use_{symbol}")
    assert edge["resolved"] is True
    assert edge["dst_id"] in {
        node["id"] for node in graph.find_nodes(symbol)
        if node["path"] == f"src/{symbol}.c"
    }
    assert edge["resolution_basis"] == "visible_declaration"


@requires_ts_c_cpp
def test_visible_declaration_must_match_definition_namespace(tmp_path):
    _write(
        tmp_path,
        "include/api.hpp",
        "namespace service { int helper(void); }\n",
    )
    _write(tmp_path, "src/global.cpp", "int helper(void) { return 1; }\n")
    _write(
        tmp_path,
        "src/user.cpp",
        '#include "api.hpp"\n'
        "namespace service { int run(void) { return helper(); } }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/user.cpp", "run")
    assert edge["resolved"] is False
    assert edge["unresolved_target"] == "helper"
    assert edge["resolution_basis"] == "syntactic_only"


@requires_ts_c_cpp
def test_visible_header_static_inline_resolves_for_including_translation_unit(tmp_path):
    _write(
        tmp_path,
        "include/registers.h",
        "#ifndef REGISTERS_H\n#define REGISTERS_H\n"
        "static inline int read_status(void) { return 7; }\n"
        "#endif\n",
    )
    _write(
        tmp_path,
        "src/user.c",
        '#include "registers.h"\nint poll(void) { return read_status(); }\n',
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/user.c", "poll")
    assert edge["resolved"] is True
    assert edge["dst_name"] == "read_status"
    assert edge["resolution_basis"] == "visible_header_inline"


@requires_ts_c_cpp
def test_python_incremental_change_cannot_degrade_c_resolution(tmp_path):
    _write_equivalence_repo(tmp_path)
    _write(tmp_path, "helpers.py", "def unrelated():\n    return 0\n")
    graph = CodeGraph(str(tmp_path))
    graph.build()
    [before] = _source_edges(graph, "src/user.c", "user_entry")
    assert before["resolved"] is True

    _write(tmp_path, "helpers.py", "def api_call():\n    return 0\n")
    code_rag.invalidate_scan_cache(tmp_path)
    graph.ensure_fresh()

    [after] = _source_edges(graph, "src/user.c", "user_entry")
    assert after["resolved"] is True
    assert after["resolution_basis"] == "visible_declaration"


def _write_equivalence_repo(root: Path) -> None:
    _write(root, "include/api.h", "int api_call(void);\n")
    _write(root, "src/api.c", "int api_call(void) { return 1; }\n")
    _write(
        root,
        "src/user.c",
        '#include "api.h"\nint user_entry(void) { return api_call(); }\n',
    )


def _snapshot(graph: CodeGraph) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(graph.db_file)
    try:
        return {
            table: sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
            for table in ("files", "nodes", "declarations", "edges")
        }
    finally:
        conn.close()


@requires_ts_c_cpp
def test_c_add_change_delete_matches_full_rebuild(tmp_path, capsys):
    incremental_root = tmp_path / "incremental"
    full_root = tmp_path / "full"
    incremental_root.mkdir()
    full_root.mkdir()
    _write_equivalence_repo(incremental_root)
    _write_equivalence_repo(full_root)

    incremental = CodeGraph(str(incremental_root))
    rebuilt = CodeGraph(str(full_root))
    incremental.build()
    rebuilt.build()
    assert _snapshot(incremental) == _snapshot(rebuilt)

    def refresh_and_compare() -> None:
        code_rag.invalidate_scan_cache(incremental_root)
        code_rag.invalidate_scan_cache(full_root)
        incremental.ensure_fresh()
        rebuilt.build()
        assert _snapshot(incremental) == _snapshot(rebuilt)

    # declaration visibility change
    for root in (incremental_root, full_root):
        _write(root, "include/api.h", "int renamed_api_call(void);\n")
    refresh_and_compare()
    assert "C/C++ visibility changed" in capsys.readouterr().err
    [unresolved] = _source_edges(incremental, "src/user.c", "user_entry")
    assert unresolved["resolved"] is False

    # add a competing external definition, then delete it again
    for root in (incremental_root, full_root):
        _write(root, "src/second.c", "int api_call(void) { return 2; }\n")
    refresh_and_compare()
    for root in (incremental_root, full_root):
        (root / "src/second.c").unlink()
    refresh_and_compare()


# ---------------------------------------------------------------------------
# 條件式 include:不得當成無條件可見性
# ---------------------------------------------------------------------------
def _conditional_repo(tmp_path):
    _write(
        tmp_path,
        "include/service.h",
        "#ifndef SERVICE_H\n#define SERVICE_H\n"
        "int service(void);\n#endif\n",
    )
    _write(
        tmp_path,
        "src/service.c",
        '#include "service.h"\nint service(void) { return 1; }\n',
    )


@requires_ts_c_cpp
@pytest.mark.smoke
def test_conditional_include_is_only_a_conditional_candidate(tmp_path):
    """`#ifdef FEATURE` 內的 include + branch 外的呼叫 → 不可 resolved。"""
    _conditional_repo(tmp_path)
    _write(
        tmp_path,
        "src/user.c",
        "#ifdef FEATURE\n"
        '#include "service.h"\n'
        "#endif\n"
        "int run(void) { return service(); }\n",
    )

    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/user.c", "run")
    assert edge["resolved"] is False
    assert edge["resolution_basis"] == "conditional_candidate"
    assert edge["ambiguity_group"] is not None

    conn = sqlite3.connect(graph.db_file)
    include_condition = conn.execute(
        "SELECT condition FROM edges"
        " WHERE src_id='src/user.c' AND type='includes'"
    ).fetchone()
    conn.close()
    # include edge 必須保存自己的 condition,否則 closure 無從判斷相容性。
    assert include_condition == ("#ifdef FEATURE",)


@requires_ts_c_cpp
def test_call_inside_the_same_branch_still_resolves(tmp_path):
    """同一個 `#ifdef FEATURE` 內的呼叫仍然看得到該 include → 正常解析。"""
    _conditional_repo(tmp_path)
    _write(
        tmp_path,
        "src/user_same_branch.c",
        "#ifdef FEATURE\n"
        '#include "service.h"\n'
        "int run_same(void) { return service(); }\n"
        "#endif\n",
    )

    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/user_same_branch.c", "run_same")
    assert edge["resolved"] is True
    assert edge["dst_name"] == "service"
    assert edge["resolution_basis"] == "visible_declaration"


@requires_ts_c_cpp
def test_transitive_conditional_include_does_not_grant_visibility(tmp_path):
    """無條件 include 的 header 裡再條件式 include → 整條路徑降為候選。"""
    _conditional_repo(tmp_path)
    _write(
        tmp_path,
        "include/facade.h",
        "#ifndef FACADE_H\n#define FACADE_H\n"
        "#ifdef FEATURE\n"
        '#include "service.h"\n'
        "#endif\n"
        "#endif\n",
    )
    _write(
        tmp_path,
        "src/transitive.c",
        '#include "facade.h"\n'
        "int run_transitive(void) { return service(); }\n",
    )

    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/transitive.c", "run_transitive")
    assert edge["resolved"] is False
    assert edge["resolution_basis"] == "conditional_candidate"


@requires_ts_c_cpp
def test_conditional_static_inline_header_is_not_visible(tmp_path):
    """條件式 include 的 static-inline header 定義同樣不算 translation unit 的一部分。"""
    _write(
        tmp_path,
        "include/regs.h",
        "#ifndef REGS_H\n#define REGS_H\n"
        "static inline int read_status(void) { return 7; }\n#endif\n",
    )
    _write(
        tmp_path,
        "src/poll.c",
        "#ifdef FEATURE\n"
        '#include "regs.h"\n'
        "#endif\n"
        "int poll(void) { return read_status(); }\n",
    )

    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/poll.c", "poll")
    assert edge["resolved"] is False
    assert edge["resolution_basis"] == "conditional_candidate"


@requires_ts_c_cpp
@pytest.mark.smoke
@pytest.mark.parametrize(
    ("rel_path", "symbol", "body"),
    [
        (
            "src/nested.c",
            "run_nested",
            "#ifdef FEATURE\n"
            '#include "service.h"\n'
            "#ifdef MODE\n"
            "int run_nested(void) { return service(); }\n"
            "#endif\n"
            "#endif\n",
        ),
        (
            "src/cross.c",
            "run_cross",
            "#ifdef MODE\n"
            '#include "service.h"\n'
            "#endif\n"
            "#ifdef FEATURE\n"
            "#ifdef MODE\n"
            "int run_cross(void) { return service(); }\n"
            "#endif\n"
            "#endif\n",
        ),
    ],
    ids=[
        "nested_branch_sees_outer_conditional_include",
        "implication_is_not_limited_to_textual_prefix",
    ],
)
def test_conditional_include_is_visible_where_its_condition_is_implied(
    tmp_path, rel_path, symbol, body,
):
    """條件式 include 的條件在呼叫處確定成立 → 必須是可見,不是條件候選。

    * nested_branch_sees_outer_conditional_include:include 在 `#ifdef FEATURE`、
      呼叫在其內層 `#ifdef MODE` → 外層條件已成立。
    * implication_is_not_limited_to_textual_prefix:include 在 `#ifdef MODE`、
      呼叫在 `#ifdef FEATURE > #ifdef MODE`。巢狀順序不同,但 MODE 在呼叫處確定
      成立 → 必須是可見,不是條件候選。
    """
    _conditional_repo(tmp_path)
    _write(tmp_path, rel_path, body)

    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, rel_path, symbol)
    assert edge["resolved"] is True
    assert edge["dst_name"] == "service"
    assert edge["resolution_basis"] == "visible_declaration"


@requires_ts_c_cpp
@pytest.mark.smoke
def test_header_included_in_both_branches_stays_visible_in_each(tmp_path):
    """同一 header 在 `#ifdef` 與 `#else` 各 include 一次 → 兩邊的呼叫都看得到。

    只保留第一個 condition 會讓 `#else` 那邊被誤判成互斥而消失。
    """
    _conditional_repo(tmp_path)
    _write(
        tmp_path,
        "src/branches.c",
        "#ifdef USE_A\n"
        '#include "service.h"\n'
        "int run_a(void) { return service(); }\n"
        "#else\n"
        '#include "service.h"\n'
        "int run_b(void) { return service(); }\n"
        "#endif\n",
    )

    graph = CodeGraph(str(tmp_path))
    graph.build()

    for symbol in ("run_a", "run_b"):
        [edge] = _source_edges(graph, "src/branches.c", symbol)
        assert edge["resolved"] is True, symbol
        assert edge["dst_name"] == "service", symbol
        assert edge["resolution_basis"] == "visible_declaration", symbol


@requires_ts_c_cpp
@pytest.mark.smoke
def test_opposite_branch_of_the_same_macro_stays_excluded(tmp_path):
    """反向保命:`#ifdef MODE` 的 include 對 `#ifdef MODE > #else` 的呼叫無效。"""
    _conditional_repo(tmp_path)
    _write(
        tmp_path,
        "src/opposite.c",
        "#ifdef MODE\n"
        '#include "service.h"\n'
        "int run_on(void) { return service(); }\n"
        "#else\n"
        "int run_off(void) { return service(); }\n"
        "#endif\n",
    )

    graph = CodeGraph(str(tmp_path))
    graph.build()

    [on_edge] = _source_edges(graph, "src/opposite.c", "run_on")
    assert on_edge["resolved"] is True

    # `#else` 分支證明 include 不成立 → 連 conditional 候選都不該給,
    # 因為那個 header 在這個 branch 裡確定不存在。
    [off_edge] = _source_edges(graph, "src/opposite.c", "run_off")
    assert off_edge["resolved"] is False
    assert off_edge["unresolved_target"] == "service"
    assert off_edge["resolution_basis"] == "syntactic_only"


# ── 原 test_ast_parser_cpp.py:C/C++ tree-sitter parser golden tests(§6.3)──
def _parse(name: str, content: str):
    return parse_file(Path(name), content)


# ============================================================
# 多行 signature(regex 抓不到的核心場景)
# ============================================================
@requires_ts_c_cpp_pinned
def test_multiline_signature_c_function_is_extracted():
    content = (
        "#include <stdint.h>\n"
        "\n"
        "int drain_pending(\n"
        "    uint32_t max_events,\n"
        "    uint32_t *processed_out)\n"
        "{\n"
        "    return 0;\n"
        "}\n"
    )
    symbols = _parse("m.c", content)
    names = [s.name for s in symbols]
    assert names == ["drain_pending"]
    sym = symbols[0]
    assert sym.start_line == 3
    assert sym.end_line == 8
    assert sym.backend == "tree-sitter"
    # 最小 signature(§6.2-3):首行到 '{' 前,含完整參數列
    assert sym.signature is not None
    assert "max_events" in sym.signature and "processed_out" in sym.signature
    assert "{" not in sym.signature


# ============================================================
# nested namespace/class:重複 node rate = 0(§6.2-1 回歸)
# ============================================================
@requires_ts_c_cpp_pinned
def test_nested_class_methods_are_not_duplicated():
    content = (
        "namespace outer {\n"
        "class Widget {\n"
        "public:\n"
        "    int area() {\n"
        "        return 4;\n"
        "    }\n"
        "    int perimeter() {\n"
        "        return 8;\n"
        "    }\n"
        "};\n"
        "}\n"
    )
    symbols = _parse("m.cpp", content)
    from collections import Counter

    counts = Counter((s.name, s.type) for s in symbols)
    dup = {k: v for k, v in counts.items() if v > 1}
    assert not dup, f"重複 node(雙重遞迴回歸): {dup}"
    by_name = {s.name: s for s in symbols}
    assert by_name["area"].type == "method"
    assert by_name["area"].parent == "Widget"
    assert by_name["perimeter"].type == "method"
    # method 不得再以 'function' 身分多出一份
    assert sum(1 for s in symbols if s.name == "area") == 1


@requires_ts_c_cpp_pinned
def test_qualified_name_chain_namespace_class_method():
    content = (
        "namespace ns {\n"
        "namespace inner {\n"
        "class A {\n"
        "public:\n"
        "    void run() {\n"
        "    }\n"
        "};\n"
        "}\n"
        "}\n"
    )
    symbols = _parse("m.cpp", content)
    by_name = {s.name: s for s in symbols}
    assert by_name["ns"].qualified_name == "ns"
    assert by_name["inner"].qualified_name == "ns::inner"
    assert by_name["A"].qualified_name == "ns::inner::A"
    assert by_name["run"].qualified_name == "ns::inner::A::run"
    assert by_name["run"].parent == "A", "parent 維持 immediate parent,不是全鏈"


# ============================================================
# template / duplicate decl+def / malformed / UTF-8 / node range
# ============================================================
@requires_ts_c_cpp_pinned
def test_basic_template_function_and_class():
    content = (
        "template <typename T>\n"
        "T biggest(T a, T b) {\n"
        "    return a > b ? a : b;\n"
        "}\n"
        "\n"
        "template <class U>\n"
        "class Holder {\n"
        "public:\n"
        "    U get() {\n"
        "        return value;\n"
        "    }\n"
        "    U value;\n"
        "};\n"
    )
    symbols = _parse("m.cpp", content)
    names = {s.name for s in symbols}
    assert "biggest" in names
    assert "Holder" in names
    assert "get" in names
    from collections import Counter

    counts = Counter((s.name, s.type) for s in symbols)
    assert not {k: v for k, v in counts.items() if v > 1}, "template 內不得有重複 node"


@requires_ts_c_cpp_pinned
def test_declaration_is_not_a_definition():
    content = (
        "int compute(int a);\n"          # 宣告:不抽
        "\n"
        "int compute(int a) {\n"          # 定義:抽
        "    return a * 2;\n"
        "}\n"
    )
    symbols = _parse("m.c", content)
    computes = [s for s in symbols if s.name == "compute"]
    assert len(computes) == 1, "宣告與定義只有定義入索引"
    assert computes[0].start_line == 3


@requires_ts_c_cpp_pinned
@pytest.mark.smoke
def test_struct_reference_is_not_a_definition():
    """BUG REGRESSION:型別**引用**與 forward tag 被當成 struct definition。

    重現(施工規格 §3 洞 1b):

        struct nowhere_defined_ops;
        extern struct nowhere_defined_ops g_ops;

    修正前輸出 2 個 symbol,兩個都是 `struct nowhere_defined_ops` —— 一個來自
    forward tag declaration,一個來自 extern 宣告裡的型別引用。兩者都不是定義,
    而真正該被記錄的 file-scope 物件反而完全沒抽到。假定義是 precision 債:
    檢索會把「這個 struct 定義在這裡」的錯誤證據餵給模型。
    """
    forward_only = (
        "struct nowhere_defined_ops;\n"
        "extern struct nowhere_defined_ops g_ops;\n"
    )
    assert _parse("a.c", forward_only) == [], (
        "forward tag declaration 與 extern 宣告都不產生定義"
    )

    pointer_object = (
        "struct referenced_ops;\n"
        "static const struct referenced_ops *table;\n"
    )
    symbols = _parse("b.c", pointer_object)
    assert [(sym.type, sym.name) for sym in symbols] == [("global", "table")], (
        "型別引用不是 struct definition;真正的 file-scope 物件 table 才是"
    )
    assert symbols[0].linkage == "internal"


@requires_ts_c_cpp_pinned
def test_c_definition_linkage_and_preprocessor_condition_are_preserved():
    content = (
        "static int local_helper(void) { return 1; }\n"
        "#if defined(BOARD_ALPHA)\n"
        "int variant_init(void) { return 2; }\n"
        "#else\n"
        "int variant_init(void) { return 3; }\n"
        "#endif\n"
    )
    symbols = _parse("variant.c", content)
    local = next(sym for sym in symbols if sym.name == "local_helper")
    variants = [sym for sym in symbols if sym.name == "variant_init"]

    assert local.linkage == "internal"
    assert local.condition is None
    assert len(variants) == 2
    assert all(sym.linkage == "external" for sym in variants)
    assert variants[0].condition == "#if defined(BOARD_ALPHA)"
    assert variants[1].condition == "#if defined(BOARD_ALPHA) > #else"


@requires_ts_c_cpp_pinned
def test_cpp_static_member_is_not_translation_unit_internal():
    symbols = _parse(
        "member.cpp",
        "class Device { public: static int ready(void) { return 1; } };\n",
    )
    ready = next(sym for sym in symbols if sym.name == "ready")
    assert ready.linkage == "external"


@requires_ts_c_cpp_pinned
def test_cpp_anonymous_namespace_function_is_internal():
    symbols = _parse(
        "anonymous.cpp",
        "namespace { int hidden(void) { return 1; } }\n",
    )
    hidden = next(sym for sym in symbols if sym.name == "hidden")
    assert hidden.linkage == "internal"


@requires_ts_c_cpp_pinned
def test_malformed_source_does_not_crash_and_extracts_best_effort():
    content = (
        "int ok_before(void) {\n"
        "    return 1;\n"
        "}\n"
        "int broken(int a, {\n"           # malformed
        "\n"
        "int ok_after(void) {\n"
        "    return 2;\n"
        "}\n"
    )
    symbols = _parse("m.c", content)  # 不得 raise
    names = {s.name for s in symbols}
    assert "ok_before" in names, "malformed 段落不得毀掉整檔抽取"


@requires_ts_c_cpp_pinned
def test_utf8_identifier_and_comments():
    content = (
        "// 初始化佇列(中文註解)\n"
        "int init_queue_模組(void) {\n"
        "    return 0;\n"
        "}\n"
    )
    symbols = _parse("m.c", content)  # 不得 raise;identifier 抽不抽到皆可接受
    for s in symbols:
        assert isinstance(s.name, str)


@requires_ts_c_cpp_pinned
def test_overloads_are_both_extracted_with_same_qualified_name():
    content = (
        "int scale(int v) {\n"
        "    return v;\n"
        "}\n"
        "float scale(float v) {\n"
        "    return v;\n"
        "}\n"
    )
    symbols = _parse("m.cpp", content)
    overloads = [s for s in symbols if s.name == "scale"]
    assert len(overloads) == 2, "overload 兩個定義都要抽出(stable ID 消歧在 graph 層)"
    assert {s.qualified_name for s in overloads} == {"scale"}
    sigs = {s.signature for s in overloads}
    assert len(sigs) == 2, "兩個 overload 的 signature 必須不同(ID tie-break 依賴它)"


@requires_ts_c_cpp_pinned
@pytest.mark.parametrize("extension", [".hh", ".hxx"])
def test_cpp_header_extensions_use_cpp_tree_sitter(extension):
    symbols = _parse(
        f"registers{extension}",
        "namespace device { inline int read_status(void) { return 7; } }\n",
    )
    [symbol] = [row for row in symbols if row.name == "read_status"]
    assert symbol.backend == "tree-sitter"
    assert symbol.qualified_name == "device::read_status"


@requires_ts_c_cpp_pinned
def test_every_node_range_is_within_file_bounds():
    content = (
        "namespace n {\n"
        "class C {\n"
        "public:\n"
        "    void m() {\n"
        "    }\n"
        "};\n"
        "}\n"
        "int f(\n"
        "    int a)\n"
        "{\n"
        "    return a;\n"
        "}\n"
    )
    total_lines = content.count("\n") + 1
    for sym in _parse("m.cpp", content):
        assert 1 <= sym.start_line <= sym.end_line <= total_lines, (
            f"{sym.name}: range {sym.start_line}-{sym.end_line} 超出檔案 {total_lines} 行"
        )


# ============================================================
# .h 語言判定(§6.2-2)與 parser status 誠實化(§6.2-4)
# ============================================================
@requires_ts_c_cpp_pinned
def test_h_defaults_to_c_and_the_client_json_key_overrides(monkeypatch):
    """`.h` 預設當 C;`client.json` 的 `h_lang` 可整體覆寫。

    2026-09-04:覆寫來源從 `AICODE_H_LANG` 換成 client.json 的鍵(經
    `client_config.apply_to_config()` 推進 `config.H_LANG`)。行為為什麼該變:
    它決定整個 repo 的 header 用哪個 grammar 解析,那是專案層級的一次性決定,
    不該隨殼層漂移;而且殘留值會讓同一份 repo 在兩個終端機裡解出不同結果。
    """
    import config

    monkeypatch.setenv("AICODE_H_LANG", "cpp")  # 殘留值一律無效
    monkeypatch.setattr(config, "H_LANG", "c")
    parser = ast_parser.get_parser(Path("x.h"))
    assert isinstance(parser, ast_parser.TreeSitterParser)
    assert parser.language_name == "c"

    monkeypatch.setattr(config, "H_LANG", "cpp")
    parser = ast_parser.get_parser(Path("x.h"))
    assert parser.language_name == "cpp"

    monkeypatch.setattr(config, "H_LANG", "bogus")
    with pytest.raises(ast_parser.ParserDependencyError, match="h_lang"):
        ast_parser.get_parser(Path("x.h"))


@requires_ts_c_cpp_pinned
def test_parser_status_reports_python_ast_and_ts_backends():
    status = get_parser_status()
    languages = status["languages"]
    assert languages["python"] == "python-ast", "python 恆為 stdlib ast,不受 tree-sitter 影響"
    assert languages["c"] == "tree-sitter"
    assert languages["cpp"] == "tree-sitter"
    # 沒裝 grammar 的語言不能再宣稱有 regex 替代；使用時必須直接報錯。
    for lang in ("go", "rust"):
        assert languages[lang] in ("tree-sitter", "unavailable")
    assert languages["java"] in ("ctags", "unavailable")


# ============================================================
# C/C++ definition 語意(施工規格 §6 P2-2)
# 全部 NEW SILENT CONTRACT:壞掉不會有紅字,只會是索引裡少了東西 /
# 多了假定義,而檢索品質的退步查不到源頭。
# ============================================================
@requires_ts_c_cpp_pinned
@pytest.mark.smoke
def test_firmware_top_level_entities_are_all_indexed():
    """韌體 C 檔的暫存器位址、timeout、feature gate、狀態機、ops table、全域旗標。

    修正前 11 個 top-level 實體只有 2 個進得了索引(而且那 2 個裡還有一個是
    假的 struct 定義)。掉的正好是韌體 repo 最需要被檢索到的那一類。
    """
    content = (
        "#define UART_BASE_ADDR 0x40001000\n"
        "#define WDT_TIMEOUT_MS 500\n"
        "#define LOG_ERR(fmt) log_write(2, fmt)\n"
        "typedef struct { volatile uint32_t dr; } uart_regs_t;\n"
        "typedef enum { STATE_IDLE = 0, STATE_BUSY = 1 } link_state_t;\n"
        "struct driver_ops;\n"
        "static const struct driver_ops uart_ops = {0};\n"
        "uint32_t g_error_counter;\n"
        "void uart_init(void) { }\n"
        "int uart_send(const char *b) { return 0; }\n"
    )
    found = {(sym.type, sym.name) for sym in _parse("fw.c", content)}
    assert found == {
        ("macro", "UART_BASE_ADDR"),
        ("macro", "WDT_TIMEOUT_MS"),
        ("macro_function", "LOG_ERR"),
        ("typedef", "uart_regs_t"),
        ("typedef", "link_state_t"),
        ("enum", "link_state_t"),
        ("enum_constant", "STATE_IDLE"),
        ("enum_constant", "STATE_BUSY"),
        ("global", "uart_ops"),
        ("global", "g_error_counter"),
        ("function", "uart_init"),
        ("function", "uart_send"),
    }


@requires_ts_c_cpp_pinned
@pytest.mark.smoke
def test_multi_declarator_globals_and_typedefs_each_produce_a_symbol():
    """一條宣告有多個 declarator 時要逐一產生 symbol,不是只取第一個。"""
    symbols = _parse(
        "multi.c",
        "typedef int count_t, *count_ptr_t;\n"
        "static int a, *b, arr[4];\n",
    )
    assert [(sym.type, sym.name) for sym in symbols] == [
        ("typedef", "count_t"),
        ("typedef", "count_ptr_t"),
        ("global", "a"),
        ("global", "b"),
        ("global", "arr"),
    ]
    assert all(sym.linkage == "internal" for sym in symbols if sym.type == "global")


@requires_ts_c_cpp_pinned
@pytest.mark.smoke
def test_extern_without_initializer_is_declaration_but_with_one_is_definition():
    """C 的 tentative definition / 純宣告 / extern+initializer 三者要分得開。"""
    symbols = _parse(
        "linkage.c",
        "uint32_t g_error_counter;\n"      # tentative definition
        "extern int only_declared;\n"      # 純宣告
        "extern int defined_here = 1;\n",  # 有 initializer:是定義
    )
    assert [(sym.type, sym.name) for sym in symbols] == [
        ("global", "g_error_counter"),
        ("global", "defined_here"),
    ]
    assert symbols[0].linkage == "external"
    assert symbols[1].storage_class == "extern"


@requires_ts_c_cpp_pinned
@pytest.mark.smoke
def test_anonymous_typedef_enum_uses_the_alias_and_parents_its_enumerators():
    """匿名 enum 不假設 AST 有 enum name;有 typedef alias 就用 alias。"""
    symbols = _parse("e.c", "typedef enum { IDLE, BUSY } state_t;\n")
    kinds = {(sym.type, sym.name, sym.parent) for sym in symbols}
    assert kinds == {
        ("typedef", "state_t", None),
        ("enum", "state_t", None),
        ("enum_constant", "IDLE", "state_t"),
        ("enum_constant", "BUSY", "state_t"),
    }

    # 完全匿名(沒有 alias)時不得造假名字,enumerator 的 parent 明確是 None。
    bare = _parse("e2.c", "enum { LONE_A, LONE_B };\n")
    assert [(sym.type, sym.name, sym.parent) for sym in bare] == [
        ("enum_constant", "LONE_A", None),
        ("enum_constant", "LONE_B", None),
    ]


@requires_ts_c_cpp_pinned
@pytest.mark.smoke
def test_struct_and_class_need_a_body_to_be_a_type_definition():
    """沒有 field_declaration_list 就不是型別定義,只是 forward tag 或引用。"""
    assert _parse("s.c", "struct opaque_t;\nunion other_u;\nenum color_e;\n") == []
    with_body = _parse("s2.c", "struct with_body { int x; };\n")
    assert [(sym.type, sym.name) for sym in with_body] == [("struct", "with_body")]


@requires_ts_c_cpp_pinned
@pytest.mark.smoke
def test_prototypes_members_locals_and_parameters_are_excluded():
    """只處理 translation-unit / namespace scope 的定義。"""
    symbols = _parse(
        "scope.c",
        "int prototype_only(int x);\n"
        "struct holder { int member_field; };\n"
        "void fn(int param) { int local_var; static int local_static; }\n",
    )
    names = {sym.name for sym in symbols}
    assert names == {"holder", "fn"}
    for excluded in ("prototype_only", "member_field", "param",
                     "local_var", "local_static"):
        assert excluded not in names


@requires_ts_c_cpp_pinned
@pytest.mark.smoke
def test_function_pointer_object_is_a_global_not_a_prototype():
    """`int (*handler)(int);` 是物件定義。韌體的 ops table / callback slot 靠這條。"""
    symbols = _parse(
        "ops.c",
        "static int (*handler)(int);\n"
        "static int (*const ops_table[4])(void);\n",
    )
    assert [(sym.type, sym.name) for sym in symbols] == [
        ("global", "handler"),
        ("global", "ops_table"),
    ]


@requires_ts_c_cpp_pinned
@pytest.mark.smoke
def test_cpp_object_linkage_narrows_instead_of_guessing_external():
    """C++ namespace-scope const 是 internal;證明不了的標 unknown,不猜 external。"""
    symbols = {
        sym.name: sym
        for sym in _parse(
            "obj.cpp",
            "const int kInternalConst = 5;\n"
            "constexpr int kInternalConstexpr = 6;\n"
            "extern const int kExternalConst = 7;\n"
            "inline int kInlineVar = 8;\n"
            "int kPlainExternal = 9;\n"
            "volatile const int kVolatileConst = 11;\n"
            "thread_local int kThreadLocal = 12;\n"
            "namespace { int hidden_obj = 1; }\n",
        )
    }
    assert symbols["kInternalConst"].linkage == "internal"
    assert symbols["kInternalConstexpr"].linkage == "internal"
    assert symbols["kExternalConst"].linkage == "external"
    assert symbols["kInlineVar"].linkage == "external"
    assert symbols["kPlainExternal"].linkage == "external"
    # volatile 讓 const-implies-internal 的規則失效。
    assert symbols["kVolatileConst"].linkage == "external"
    assert symbols["kThreadLocal"].linkage == "unknown"
    assert symbols["hidden_obj"].linkage == "internal"


@requires_ts_c_cpp_pinned
@pytest.mark.smoke
def test_preprocessor_condition_is_kept_for_new_definition_kinds():
    """#if 兩臂的同名 macro / global 都要保留各自的 condition,不得合併或丟失。"""
    symbols = _parse(
        "variant.c",
        "#if defined(BOARD_ALPHA)\n"
        "#define TIMEOUT_MS 100\n"
        "static int variant_flag = 1;\n"
        "#else\n"
        "#define TIMEOUT_MS 200\n"
        "static int variant_flag = 2;\n"
        "#endif\n",
    )
    conditions = [(sym.type, sym.name, sym.condition) for sym in symbols]
    assert conditions == [
        ("macro", "TIMEOUT_MS", "#if defined(BOARD_ALPHA)"),
        ("global", "variant_flag", "#if defined(BOARD_ALPHA)"),
        ("macro", "TIMEOUT_MS", "#if defined(BOARD_ALPHA) > #else"),
        ("global", "variant_flag", "#if defined(BOARD_ALPHA) > #else"),
    ]


@requires_ts_c_cpp_pinned
@pytest.mark.smoke
def test_scoped_enum_enumerators_are_qualified_by_their_enum():
    """BUG REGRESSION:`enum class` 的 enumerator 少了 enum scope 前綴。

    C++ scoped enum 的 enumerator 是 `State::Idle`,不是 `Idle`。固定產生裸名的話,
    兩個 scoped enum 只要有同名 enumerator 就會撞成同一個 qualified name ——
    graph 的 stable node ID 依賴 qualified_name,撞名等於查找結果不準。
    unscoped enum 相反:enumerator 本來就在外層 scope,維持裸名才對。
    """
    symbols = _parse(
        "enums.cpp",
        "enum class State { Idle, Busy };\n"
        "enum struct Mode { Idle, Fast };\n"
        "enum Plain { PlainA };\n",
    )
    qualified = {
        (sym.name, sym.parent): sym.qualified_name
        for sym in symbols if sym.type == "enum_constant"
    }
    assert qualified[("Idle", "State")] == "State::Idle"
    assert qualified[("Idle", "Mode")] == "Mode::Idle"
    assert qualified[("Idle", "State")] != qualified[("Idle", "Mode")], (
        "兩個 scoped enum 的同名 enumerator 不得撞成同一個 qualified name"
    )
    # unscoped enum:enumerator 在外層 scope,不加前綴。
    assert qualified[("PlainA", "Plain")] == "PlainA"


# ── 原 test_definition_metadata_propagation.py:definition metadata 全鏈路保存(§6 P2-4)──
FIRMWARE_C = (
    "#define WDT_TIMEOUT_MS 500\n"
    "typedef enum { LINK_IDLE, LINK_BUSY } link_state_t;\n"
    "static int internal_counter;\n"
    "int exported_counter;\n"
    "#if defined(BOARD_ALPHA)\n"
    "static int variant_flag = 1;\n"
    "#endif\n"
    "int firmware_entry(void) { return 0; }\n"
)


@pytest.mark.smoke
@pytest.mark.skipif(not HAS_TS_C, reason="tree-sitter c 未安裝")
def test_symbol_dict_keeps_linkage_condition_and_storage_class(tmp_path: Path):
    _write(tmp_path, "src/fw.c", FIRMWARE_C)
    rag = code_rag.CodeRAG(str(tmp_path))
    symbols = {
        sym["symbol"]: sym
        for sym in rag._extract_symbols(tmp_path / "src/fw.c", FIRMWARE_C)
    }

    assert symbols["internal_counter"]["linkage"] == "internal"
    assert symbols["internal_counter"]["storage_class"] == "static"
    assert symbols["exported_counter"]["linkage"] == "external"
    assert symbols["variant_flag"]["condition"] == "#if defined(BOARD_ALPHA)"
    # linkage 沒有意義的 kind 不得硬掰一個值。
    assert "linkage" not in symbols["WDT_TIMEOUT_MS"]
    assert "linkage" not in symbols["link_state_t"]


@pytest.mark.smoke
@pytest.mark.skipif(not HAS_TS_C, reason="tree-sitter c 未安裝")
def test_index_entry_persists_definition_metadata(tmp_path: Path):
    _write(tmp_path, "src/fw.c", FIRMWARE_C)
    rag = code_rag.CodeRAG(str(tmp_path))
    entries, _embeddings = rag._index_single_file(
        tmp_path / "src/fw.c", "src/fw.c", compute_embeddings=False
    )
    by_symbol = {entry["symbol"]: entry for entry in entries}

    assert by_symbol["internal_counter"]["linkage"] == "internal"
    assert by_symbol["internal_counter"]["storage_class"] == "static"
    assert by_symbol["variant_flag"]["condition"] == "#if defined(BOARD_ALPHA)"
    assert {"macro", "typedef", "enum", "enum_constant", "global", "function"} <= {
        entry["type"] for entry in entries
    }


@pytest.mark.smoke
@pytest.mark.skipif(not HAS_TS_C, reason="tree-sitter c 未安裝")
def test_parser_semantics_version_invalidates_the_coderag_cache(tmp_path: Path,
                                                                monkeypatch):
    """改 parser 語意後不得沿用舊 cache —— 舊 cache 的 symbol 集合已經是錯的。"""
    _write(tmp_path, "src/fw.c", FIRMWARE_C)
    rag = code_rag.CodeRAG(str(tmp_path))
    rag.index, _ = rag._index_single_file(
        tmp_path / "src/fw.c", "src/fw.c", compute_embeddings=False
    )
    rag._file_cache = {"src/fw.c": {"hash": "x", "symbols": rag.index,
                                    "embeddings": []}}
    rag._save_cache()

    assert code_rag.CodeRAG(str(tmp_path))._load_file_cache(), "同版本應載入得到"

    monkeypatch.setattr(code_rag, "PARSER_SEMANTICS_VERSION",
                        ast_parser.PARSER_SEMANTICS_VERSION + 1)
    assert code_rag.CodeRAG(str(tmp_path))._load_file_cache() == {}, (
        "parser semantics 一動,舊 cache 必須被判定為不可用"
    )


@pytest.mark.smoke
@pytest.mark.skipif(not HAS_TS_C, reason="tree-sitter c 未安裝")
def test_embed_text_schema_version_invalidates_the_coderag_cache(tmp_path: Path,
                                                                 monkeypatch):
    """改 embed text 的 render 也要讓舊向量失效 —— 增量重建只比 file_hash。"""
    _write(tmp_path, "src/fw.c", FIRMWARE_C)
    rag = code_rag.CodeRAG(str(tmp_path))
    rag.index, _ = rag._index_single_file(
        tmp_path / "src/fw.c", "src/fw.c", compute_embeddings=False
    )
    rag._file_cache = {"src/fw.c": {"hash": "x", "symbols": rag.index,
                                    "embeddings": []}}
    rag._save_cache()

    monkeypatch.setattr(code_rag, "EMBED_TEXT_SCHEMA_VERSION",
                        code_rag.EMBED_TEXT_SCHEMA_VERSION + 1)
    assert code_rag.CodeRAG(str(tmp_path))._load_file_cache() == {}


@pytest.mark.smoke
@pytest.mark.skipif(not HAS_TS_C, reason="tree-sitter c 未安裝")
def test_graph_keeps_parser_linkage_for_global_objects(tmp_path: Path):
    """graph node 也要留住 linkage;舊版對非 callable 一律 not_applicable。"""
    _write(tmp_path, "src/fw.c", FIRMWARE_C)
    graph = CodeGraph(str(tmp_path))
    graph.build(verbose=False)
    try:
        internal = [n for n in graph.find_nodes("internal_counter")
                    if n["path"] == "src/fw.c"]
        exported = [n for n in graph.find_nodes("exported_counter")
                    if n["path"] == "src/fw.c"]
        macro = [n for n in graph.find_nodes("WDT_TIMEOUT_MS")
                 if n["path"] == "src/fw.c"]
        variant = [n for n in graph.find_nodes("variant_flag")
                   if n["path"] == "src/fw.c"]

        assert internal and internal[0]["linkage"] == "internal"
        assert exported and exported[0]["linkage"] == "external"
        assert variant and variant[0]["condition"] == "#if defined(BOARD_ALPHA)"
        # macro 在 C/C++ 語意上沒有 linkage,誠實標 not_applicable。
        assert macro and macro[0]["linkage"] == "not_applicable"
    finally:
        graph.close()


@pytest.mark.smoke
@pytest.mark.skipif(not HAS_TS_C, reason="tree-sitter c 未安裝")
def test_parser_semantics_version_is_in_the_graph_fingerprint(tmp_path: Path):
    """語意版本要進 graph 指紋,但**不得**濫 bump GRAPH_SCHEMA_VERSION。"""
    graph = CodeGraph(str(tmp_path))
    try:
        fingerprint = graph._parser_versions()
    finally:
        graph.close()
    assert f"parser-semantics:{ast_parser.PARSER_SEMANTICS_VERSION}" in fingerprint
