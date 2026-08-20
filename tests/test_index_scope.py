"""索引範圍(index_scope)—— 成員資格、三態剪枝、Layer C loader、快取遷移。

fixture 一律用合成樹名(toolchain_x / vendor_env / ...):真實專案名永遠不進 repo。

最重要的一條是 test_tri_state_walk_matches_should_index_file:三態走訪的結果
必須等於「對全樹逐檔跑 should_index_file」。任何 PRUNE 吃掉了應該進索引的檔案,
那條就會紅 —— 這是整份設計的防呆核心。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import index_scope  # noqa: E402
from index_scope import (  # noqa: E402
    INDEX,
    PRUNE,
    TRAVERSE_ONLY,
    IndexScope,
    IndexScopeError,
    compile_pattern,
    literal_prefix,
    load_index_scope,
    load_scope_config,
    walk_index_files,
)

# ============================================================
# 合成樹
# ============================================================

TREE_FILES = {
    "src/core/engine.c": "int engine(void) { return 0; }\n",
    "src/core/engine.h": "int engine(void);\n",
    "docs/notes.md": "# notes\n",
    "vendor_env/keep.c": "int keep(void) { return 1; }\n",
    "vendor_env/junk.c": "int junk(void) { return 2; }\n",
    "vendor_env/deep/more.c": "int more(void) { return 3; }\n",
    "toolchain_x/arc/lib/src/stl/vector.h": "template<class T> struct vec {};\n",
    "toolchain_x/arc/lldbac/lib/registers/regs.c": "int regs(void) { return 4; }\n",
    "lib/python3.11/stdlib_mod.py": "def stdlib_mod(): pass\n",
    "lib/python3.11/custom_patch.py": "def custom_patch(): pass\n",
    "lib/python3_tools/helper.py": "def helper(): pass\n",
    "site-packages/mypkg/core.py": "def core(): pass\n",
    "build_env/pyvenv.cfg": "home = /usr\n",
    "build_env/lib/runtime_mod.py": "def runtime_mod(): pass\n",
    "conda_env/conda-meta/history": "",
    "conda_env/runtime_pkg.py": "def runtime_pkg(): pass\n",
    "pkg_meta.egg-info/entry.py": "def entry(): pass\n",
    "vendor/keep.c": "int vendored(void) { return 5; }\n",
    ".hidden/secret.py": "def secret(): pass\n",
    ".github/workflows/ci.yml": "name: ci\n",
    "notes.txt": "plain\n",
}


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "project_tree"
    for rel, content in TREE_FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def _write_scope(tmp_path: Path, monkeypatch, root: Path, **entry) -> Path:
    """寫一份 index-scope.json 並讓 loader 指過去(0600)。"""
    payload = {"schema_version": 1, "roots": [{"root": str(root), **entry}]}
    return _write_raw_scope(tmp_path, monkeypatch, payload)


def _write_raw_scope(tmp_path: Path, monkeypatch, payload) -> Path:
    path = tmp_path / "index-scope.json"
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    monkeypatch.setenv("AICODE_INDEX_SCOPE_FILE", str(path))
    return path


def _indexed(scope: IndexScope) -> set[str]:
    return {rel for _fp, rel in walk_index_files(scope)}


def _brute_force(scope: IndexScope, root: Path) -> set[str]:
    """不剪枝、全樹枚舉,逐檔問 should_index_file —— 不變式的基準集合。"""
    out = set()
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for filename in filenames:
            abs_path = Path(dirpath) / filename
            rel = abs_path.relative_to(root).as_posix()
            if scope.should_index_file(rel):
                out.add(rel)
    return out


def test_cpp_header_extensions_are_in_default_index_scope(tmp_path):
    root = tmp_path / "headers"
    root.mkdir()
    (root / "device.hh").write_text("int read_device(void);\n", encoding="utf-8")
    (root / "registers.hxx").write_text("int read_register(void);\n", encoding="utf-8")
    scope = IndexScope(root)

    assert scope.should_index_file("device.hh") is True
    assert scope.should_index_file("registers.hxx") is True
    assert _indexed(scope) == {"device.hh", "registers.hxx"}


# ============================================================
# 不變式(整份設計的防呆核心)
# ============================================================


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"exclude": ["vendor_env/**"], "include": ["vendor_env/keep.c"]},
        {"exclude": ["toolchain_x/arc/lib/src/stl/**"]},
        {"include": ["**/registers/**"], "exclude": ["toolchain_x/**"]},
        {"detectors": False},
        {"mode": "allowlist", "include": ["src/**", "site-packages/mypkg/core.py"]},
    ],
)
def test_tri_state_walk_matches_should_index_file(tree, tmp_path, monkeypatch, entry):
    _write_scope(tmp_path, monkeypatch, tree, **entry)
    baseline = _brute_force(load_index_scope(tree), tree)
    actual = _indexed(load_index_scope(tree))
    assert actual == baseline, (
        "三態走訪與 should_index_file 不一致 —— PRUNE 吃掉了應該進索引的檔案:"
        f"漏 {sorted(baseline - actual)} / 多 {sorted(actual - baseline)}"
    )


# ============================================================
# Rescue 四測
# ============================================================


def test_rescue_explicit_file_under_excluded_dir(tree, tmp_path, monkeypatch):
    """1. exclude 整個目錄 + include 單檔 → 只有那一檔進得來。"""
    _write_scope(tmp_path, monkeypatch, tree,
                 exclude=["vendor_env/**"], include=["vendor_env/keep.c"])
    indexed = _indexed(load_index_scope(tree))
    assert "vendor_env/keep.c" in indexed
    assert "vendor_env/junk.c" not in indexed
    assert "vendor_env/deep/more.c" not in indexed


def test_rescue_direct_file_under_b_detected_dir(tree, tmp_path, monkeypatch):
    """2. B 命中的目錄底下,explicit include 的檔案救得回來。"""
    _write_scope(tmp_path, monkeypatch, tree,
                 include=["lib/python3.11/custom_patch.py"])
    indexed = _indexed(load_index_scope(tree))
    assert "lib/python3.11/custom_patch.py" in indexed
    assert "lib/python3.11/stdlib_mod.py" not in indexed


def test_rescue_direct_file_under_a_prime_dir(tree, tmp_path, monkeypatch):
    """3. A′ 命中的目錄底下,explicit include 的檔案救得回來。"""
    _write_scope(tmp_path, monkeypatch, tree,
                 include=["site-packages/mypkg/core.py"])
    indexed = _indexed(load_index_scope(tree))
    assert "site-packages/mypkg/core.py" in indexed


def test_hard_gate_layer_a_cannot_be_rescued(tree, tmp_path, monkeypatch):
    """4a. Layer A(IGNORED_DIRS)是 hard gate,include 救不回來。"""
    _write_scope(tmp_path, monkeypatch, tree, include=["vendor/keep.c"])
    scope = load_index_scope(tree)
    assert scope.should_index_file("vendor/keep.c") is False
    assert "vendor/keep.c" not in _indexed(scope)


def test_hard_gate_containment_cannot_be_rescued(tree, tmp_path, monkeypatch):
    """4b. containment 逃逸的檔案,include 也救不回來。"""
    outside = tmp_path / "outside_tree"
    outside.mkdir()
    (outside / "leak.c").write_text("int leak(void) { return 6; }\n", encoding="utf-8")
    link = tree / "escaped.c"
    try:
        link.symlink_to(outside / "leak.c")
    except (OSError, NotImplementedError):
        pytest.skip("這個環境不能建 symlink")

    _write_scope(tmp_path, monkeypatch, tree, include=["escaped.c"])
    scope = load_index_scope(tree)
    assert scope.should_index_file("escaped.c") is False
    assert "escaped.c" not in _indexed(scope)


# ============================================================
# 預設層(A′ / B)
# ============================================================


def test_default_layers_exclude_third_party_runtimes(tree):
    indexed = _indexed(load_index_scope(tree))
    assert "src/core/engine.c" in indexed
    assert "docs/notes.md" in indexed
    assert "lib/python3_tools/helper.py" in indexed, "python3_tools 不是 pythonX.Y,不該被誤殺"
    assert ".github/workflows/ci.yml" in indexed, "ALLOWED_DOT_DIRS 行為必須不變"

    assert "site-packages/mypkg/core.py" not in indexed          # A'
    assert "lib/python3.11/stdlib_mod.py" not in indexed         # B2
    assert "build_env/lib/runtime_mod.py" not in indexed         # B1 pyvenv.cfg
    assert "conda_env/runtime_pkg.py" not in indexed             # B1 conda-meta
    assert "pkg_meta.egg-info/entry.py" not in indexed           # B2 .egg-info
    assert "vendor/keep.c" not in indexed                        # A
    assert ".hidden/secret.py" not in indexed                    # dot 目錄


def test_root_level_python_version_dir_needs_lib_parent(tmp_path):
    root = tmp_path / "tree"
    (root / "python3.11").mkdir(parents=True)
    (root / "python3.11" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (root / "lib" / "python3.11").mkdir(parents=True)
    (root / "lib" / "python3.11" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    indexed = _indexed(load_index_scope(root))
    assert "python3.11/mod.py" in indexed, "沒有 lib/lib64 父段就不是 stdlib 佈局"
    assert "lib/python3.11/mod.py" not in indexed


def test_nested_python_tools_dir_is_not_killed(tmp_path):
    """`x/lib/python3_tools` 不是 pythonX.Y —— 不能被 B2 誤殺。"""
    root = tmp_path / "tree"
    for rel in ("x/lib/python3_tools/helper.py", "x/lib/python3.11/stdlib_mod.py"):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")
    indexed = _indexed(load_index_scope(root))
    assert indexed == {"x/lib/python3_tools/helper.py"}


def test_egg_info_at_root_and_deep(tmp_path):
    root = tmp_path / "tree"
    for rel in ("thing.egg-info/a.py", "src/other.egg-info/b.py", "src/real.py"):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")
    indexed = _indexed(load_index_scope(root))
    assert indexed == {"src/real.py"}


def test_detectors_false_disables_b_but_keeps_a_prime(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, detectors=False)
    scope = load_index_scope(tree)
    indexed = _indexed(scope)
    assert "lib/python3.11/stdlib_mod.py" in indexed          # B2 關掉
    assert "build_env/lib/runtime_mod.py" in indexed          # B1 關掉
    assert "pkg_meta.egg-info/entry.py" in indexed            # B2 關掉
    assert "site-packages/mypkg/core.py" not in indexed, "A′ 是通名,detectors 關不掉"


def test_detectors_toggle_changes_fingerprint(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, detectors=True)
    on = load_index_scope(tree).fingerprint
    _write_scope(tmp_path, monkeypatch, tree, detectors=False)
    off = load_index_scope(tree).fingerprint
    assert on != off


# ============================================================
# 三態 / 可達性
# ============================================================


def test_excluded_parent_with_include_becomes_traverse_only(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree,
                 exclude=["vendor_env/**"], include=["vendor_env/keep.c"])
    scope = load_index_scope(tree)
    assert scope.decide_dir("vendor_env") == TRAVERSE_ONLY
    assert scope.decide_dir("vendor_env/deep") == PRUNE
    assert scope.decide_dir("src") == INDEX


def test_excluded_dir_without_include_is_pruned(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, exclude=["vendor_env/**"])
    scope = load_index_scope(tree)
    assert scope.decide_dir("vendor_env") == PRUNE


def test_leading_wildcard_include_degrades_to_traverse_only(tree, tmp_path, monkeypatch):
    """空 literal prefix:保守 —— 所有被排除目錄降級 TRAVERSE_ONLY,並且要有警告。"""
    _write_scope(tmp_path, monkeypatch, tree,
                 exclude=["toolchain_x/**"], include=["**/registers/**"])
    scope = load_index_scope(tree)
    assert scope.decide_dir("toolchain_x") == TRAVERSE_ONLY
    assert scope.decide_dir("toolchain_x/arc/lib/src/stl") == TRAVERSE_ONLY
    assert scope.warnings, "空 prefix include 必須有警告"
    assert any("prefix" in line or "TRAVERSE_ONLY" in line for line in scope.stats_lines())
    indexed = _indexed(scope)
    assert "toolchain_x/arc/lldbac/lib/registers/regs.c" in indexed
    assert "toolchain_x/arc/lib/src/stl/vector.h" not in indexed


def test_layer_a_dir_is_pruned_even_with_include(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, include=["vendor/keep.c"])
    assert load_index_scope(tree).decide_dir("vendor") == PRUNE


# ============================================================
# allowlist
# ============================================================


def test_allowlist_indexes_only_listed_paths(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, mode="allowlist",
                 include=["src/**", "notes.txt"])
    scope = load_index_scope(tree)
    indexed = _indexed(scope)
    assert indexed == {"src/core/engine.c", "src/core/engine.h", "notes.txt"}


def test_allowlist_ancestor_chain_is_traverse_only(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, mode="allowlist",
                 include=["toolchain_x/arc/lldbac/lib/registers/**"])
    scope = load_index_scope(tree)
    assert scope.decide_dir("toolchain_x") == TRAVERSE_ONLY
    assert scope.decide_dir("toolchain_x/arc") == TRAVERSE_ONLY
    assert scope.decide_dir("toolchain_x/arc/lldbac/lib/registers") == TRAVERSE_ONLY
    assert scope.decide_dir("docs") == PRUNE


def test_allowlist_with_exclude_fails_loud(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, mode="allowlist",
                 include=["src/**"], exclude=["src/core/**"])
    with pytest.raises(IndexScopeError) as exc:
        load_scope_config(tree)
    assert "allowlist" in str(exc.value) and "denylist" in str(exc.value)


# ============================================================
# Loader:存在/不存在、schema、權限、衛生
# ============================================================


def test_missing_scope_file_is_normal_default(tree, tmp_path, monkeypatch):
    monkeypatch.setenv("AICODE_INDEX_SCOPE_FILE", str(tmp_path / "nope.json"))
    cfg = load_scope_config(tree)
    assert cfg.mode == "denylist" and cfg.detectors is True
    assert cfg.include == () and cfg.exclude == ()
    assert cfg.scope_file_present is False and cfg.selector_matched is False
    assert "C: no matching selector" not in load_index_scope(tree).stats_lines()


def test_broken_json_fails_loud(tree, tmp_path, monkeypatch):
    _write_raw_scope(tmp_path, monkeypatch, "{not json")
    with pytest.raises(IndexScopeError):
        load_scope_config(tree)


@pytest.mark.parametrize("payload", [
    {"schema_version": 2, "roots": []},
    {"schema_version": 1, "roots": [], "extra_key": 1},
    {"schema_version": 1, "roots": {}},
    {"schema_version": 1, "roots": [{"root": "relative/path"}]},
    {"schema_version": 1, "roots": [{"root": "/abs", "mode": "whitelist"}]},
    {"schema_version": 1, "roots": [{"root": "/abs", "detectors": "yes"}]},
    {"schema_version": 1, "roots": [{"root": "/abs", "unknown": 1}]},
])
def test_schema_violations_fail_loud(tree, tmp_path, monkeypatch, payload):
    _write_raw_scope(tmp_path, monkeypatch, payload)
    with pytest.raises(IndexScopeError):
        load_scope_config(tree)


def test_duplicate_selector_fails_loud(tree, tmp_path, monkeypatch):
    _write_raw_scope(tmp_path, monkeypatch, {
        "schema_version": 1,
        "roots": [{"root": str(tree)}, {"root": str(tree) + "/."}],
    })
    with pytest.raises(IndexScopeError) as exc:
        load_scope_config(tree)
    assert "重複" in str(exc.value)


def test_no_matching_selector_is_not_an_error(tree, tmp_path, monkeypatch):
    _write_raw_scope(tmp_path, monkeypatch, {
        "schema_version": 1,
        "roots": [{"root": str(tmp_path / "some_other_tree"), "exclude": ["src/**"]}],
    })
    scope = load_index_scope(tree)
    assert scope.selector_matched is False
    assert "C: no matching selector" in scope.stats_lines()
    assert "src/core/engine.c" in _indexed(scope), "沒匹配到就不該套用那組規則"


@pytest.mark.parametrize("pattern", ["!keep.c", "../escape/**", "  ", "nul\x00byte"])
def test_pattern_hygiene_fails_loud(tree, tmp_path, monkeypatch, pattern):
    _write_scope(tmp_path, monkeypatch, tree, exclude=[pattern])
    with pytest.raises(IndexScopeError):
        load_scope_config(tree)


def test_pattern_length_limit_fails_loud(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, exclude=["a" * 513])
    with pytest.raises(IndexScopeError) as exc:
        load_scope_config(tree)
    assert "過長" in str(exc.value)


def test_pattern_count_limit_fails_loud(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree,
                 exclude=[f"dir_{i}/**" for i in range(201)])
    with pytest.raises(IndexScopeError) as exc:
        load_scope_config(tree)
    assert "太多" in str(exc.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 權限實檢")
def test_world_readable_scope_file_fails_loud(tree, tmp_path, monkeypatch):
    path = _write_scope(tmp_path, monkeypatch, tree, exclude=["src/**"])
    os.chmod(path, 0o644)
    with pytest.raises(IndexScopeError) as exc:
        load_scope_config(tree)
    assert "chmod 600" in str(exc.value)


def test_selector_matching_uses_normcase(tree, tmp_path, monkeypatch):
    """Windows 上 selector 比對要吃 normcase;POSIX 上大小寫仍然有意義。"""
    _write_raw_scope(tmp_path, monkeypatch, {
        "schema_version": 1,
        "roots": [{"root": str(tree).upper(), "exclude": ["src/**"]}],
    })
    cfg = load_scope_config(tree)
    expected = os.path.normcase(str(tree)) == os.path.normcase(str(tree).upper())
    assert cfg.selector_matched is expected


# ============================================================
# Matcher 方言
# ============================================================


@pytest.mark.parametrize("pattern,path,expected", [
    # 實測 pathspec 的目錄尾斜線行為 —— 這三條是方言的定義向量
    ("vendor_env/**", "vendor_env", False),
    ("vendor_env/**", "vendor_env/", True),
    ("vendor_env/**", "vendor_env/keep.c", True),
    ("vendor_env/keep.c", "vendor_env/keep.c", True),
    ("vendor_env/keep.c", "vendor_env/keepXc", False),
    ("toolchain_x/arc/lib/src/stl/**", "toolchain_x/arc/lib/src/stl/vector.h", True),
    ("toolchain_x/arc/lib/src/stl/**", "toolchain_x/arc/lib/src/other.h", False),
    ("**/registers/**", "a/b/registers/regs.c", True),
    ("**/registers/**", "registers/regs.c", True),
    ("*.tmp", "deep/nested/x.tmp", True),
    ("/src/**", "src/core/engine.c", True),
    ("/src/**", "other/src/core/engine.c", False),
    ("src", "src/core/engine.c", True),
    ("src", "src/", True),
    ("SRC/**", "src/core/engine.c", False),          # case-sensitive
])
def test_matcher_vectors(pattern, path, expected):
    assert bool(compile_pattern(pattern).match(path)) is expected


def test_backslash_patterns_are_normalized(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, exclude=["src\\core\\**"])
    indexed = _indexed(load_index_scope(tree))
    assert "src/core/engine.c" not in indexed
    assert "docs/notes.md" in indexed


@pytest.mark.parametrize("pattern,expected", [
    ("vendor_env/**", "vendor_env"),
    ("vendor_env/keep.c", "vendor_env/keep.c"),
    ("a/b/c/**/d", "a/b/c"),
    ("**/registers/**", None),
    ("*.tmp", None),
    ("/src/**", "src"),
])
def test_literal_prefix(pattern, expected):
    assert literal_prefix(pattern) == expected


# ============================================================
# Symlink / containment
# ============================================================


def test_symlinked_dir_escape_is_dropped_and_counted(tree, tmp_path):
    outside = tmp_path / "outside_tree"
    (outside / "pkg").mkdir(parents=True)
    (outside / "pkg" / "leak.c").write_text("int leak(void);\n", encoding="utf-8")
    try:
        (tree / "linked_pkg").symlink_to(outside / "pkg", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("這個環境不能建 symlink")

    scope = load_index_scope(tree)
    indexed = _indexed(scope)
    assert not any(rel.startswith("linked_pkg") for rel in indexed)
    assert "symlink-escape skipped: 1" in scope.stats_lines()


def test_symlink_inside_root_is_not_counted_as_escape(tree, tmp_path):
    """指向 root 內的 symlink 不算逃逸(不進 escape 計數)。

    內容仍然不會被索引 —— os.walk(followlinks=False) 本來就不遞迴進 symlink
    目錄,committed 版本也是這個行為。這裡守的是「別把它誤判成逃逸」。
    """
    (tree / "src" / "shared").mkdir()
    (tree / "src" / "shared" / "shared_mod.c").write_text("int s(void);\n", encoding="utf-8")
    try:
        (tree / "alias_dir").symlink_to(tree / "src" / "shared", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("這個環境不能建 symlink")
    scope = load_index_scope(tree)
    indexed = _indexed(scope)
    assert "src/shared/shared_mod.c" in indexed
    assert "symlink-escape skipped: 0" in scope.stats_lines()


# ============================================================
# list_dir / grep 不受影響
# ============================================================


def test_list_dir_still_shows_index_excluded_dirs(tree):
    from agent_tools import ToolExecutor

    listing = ToolExecutor(str(tree)).list_files(".", depth=1)
    assert "site-packages" in listing
    assert "build_env" in listing
    indexed = _indexed(load_index_scope(tree))
    assert not any(rel.startswith("site-packages/") for rel in indexed)


def test_index_scope_is_not_wired_into_grep_or_list_dir():
    for name in ("agent_tools.py", "utils.py"):
        src = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "index_scope" not in src, f"{name} 不該碰索引範圍 —— grep/list_dir 要保持不變"


def test_index_artifacts_never_enter_the_index(tree):
    from config import CODE_RAG_CACHE_FILE

    scope = load_index_scope(tree)
    for name in index_scope.INDEX_ARTIFACT_FILES:
        (tree / name).write_text("{}", encoding="utf-8")
        assert scope.should_index_file(name) is False, name
    assert CODE_RAG_CACHE_FILE in index_scope.INDEX_ARTIFACT_FILES
    assert not any(rel in index_scope.INDEX_ARTIFACT_FILES for rel in _indexed(scope))


def test_layer_a_frozen_at_nineteen_names():
    import config

    assert len(config.IGNORED_DIRS) == 19, "Layer A 是凍結的:一字不加"


def test_stats_lines_can_hide_zero_counts(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, exclude=["vendor_env/**"])
    scope = load_index_scope(tree)
    _indexed(scope)
    quiet = scope.stats_lines(include_zero=False)
    assert "traverse-only dirs: 0" not in quiet
    assert "C#1 dirs: 1" in quiet
    assert "traverse-only dirs: 0" in scope.stats_lines()


def test_build_index_summary_is_counts_only(tree, tmp_path, monkeypatch, capsys,
                                            clean_scan_cache):
    code_rag = clean_scan_cache
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", False)
    _write_scope(tmp_path, monkeypatch, tree, exclude=["vendor_env/**"])
    rag = code_rag.CodeRAG(str(tree))
    monkeypatch.setattr(rag, "_get_embedding", lambda _text: [1.0, 0.0])
    monkeypatch.setattr(
        rag, "_embed_texts_batched", lambda texts: [[1.0, 0.0]] * len(texts)
    )
    rag.build_index(verbose=True)
    out = capsys.readouterr().out
    assert "index scope" in out
    for secret in ("vendor_env", "site-packages", "engine.c", str(tree)):
        assert secret not in out, f"建索引摘要洩漏路徑: {secret}"


def test_build_index_summary_silent_on_default_deployment(tmp_path, monkeypatch, capsys,
                                                          clean_scan_cache):
    code_rag = clean_scan_cache
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", False)
    root = tmp_path / "plain_tree"
    (root / "src").mkdir(parents=True)
    (root / "src" / "mod.py").write_text("def mod(): pass\n", encoding="utf-8")
    rag = code_rag.CodeRAG(str(root))
    monkeypatch.setattr(rag, "_get_embedding", lambda _text: [1.0, 0.0])
    monkeypatch.setattr(
        rag, "_embed_texts_batched", lambda texts: [[1.0, 0.0]] * len(texts)
    )
    rag.build_index(verbose=True)
    assert "index scope" not in capsys.readouterr().out


# ============================================================
# fingerprint
# ============================================================


def test_fingerprint_changes_with_patterns(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, exclude=["a/**"])
    first = load_index_scope(tree).fingerprint
    _write_scope(tmp_path, monkeypatch, tree, exclude=["b/**"])
    assert load_index_scope(tree).fingerprint != first


def test_fingerprint_is_order_sensitive(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, exclude=["a/**", "b/**"])
    first = load_index_scope(tree).fingerprint
    _write_scope(tmp_path, monkeypatch, tree, exclude=["b/**", "a/**"])
    assert load_index_scope(tree).fingerprint != first


def test_fingerprint_tracks_code_extensions(tree, monkeypatch):
    first = load_index_scope(tree).fingerprint
    monkeypatch.setattr(index_scope, "CODE_EXTENSIONS", set(index_scope.CODE_EXTENSIONS) | {".zig"})
    assert load_index_scope(tree).fingerprint != first


# ============================================================
# 快取遷移(§7)
# ============================================================


@pytest.fixture()
def clean_scan_cache():
    import code_rag

    code_rag._INDEX_SCAN_CACHE.clear()
    yield code_rag
    code_rag._INDEX_SCAN_CACHE.clear()


def test_scan_cache_fast_path_requires_matching_fingerprint(tree, clean_scan_cache):
    import time as _time

    code_rag = clean_scan_cache
    rag = code_rag.CodeRAG(str(tree))
    code_rag._INDEX_SCAN_CACHE[(str(rag.folder), "fingerprint-from-another-scope")] = {
        "entries": {"vendor/keep.c": "stale-hash"},
        "timestamp": _time.time(),
    }
    files = rag._scan_code_files()
    assert "vendor/keep.c" not in files
    assert "src/core/engine.c" in files, "fingerprint 不符就該重掃,不是回空的"


def test_cached_paths_are_refiltered_through_should_index_file(tree, clean_scan_cache):
    import time as _time

    code_rag = clean_scan_cache
    rag = code_rag.CodeRAG(str(tree))
    code_rag._INDEX_SCAN_CACHE[(str(rag.folder), rag.scope.fingerprint)] = {
        "entries": {
            "vendor/keep.c": "h1",
            "site-packages/mypkg/core.py": "h2",
            "src/core/engine.c": "h3",
        },
        "timestamp": _time.time(),
    }
    files = rag._scan_code_files()
    assert set(files) == {"src/core/engine.c"}
    # §5-3:TTL 內 fast path 直接回快照 hash,零 compute_file_hash
    assert files["src/core/engine.c"]["hash"] == "h3"


def _write_legacy_meta(rag, *, fingerprint, paths):
    np = pytest.importorskip("numpy")
    meta = {
        "embedding_model": __import__("code_rag").EMBEDDING_MODEL,
        "folder_hash": "legacy-folder-hash",  # pre-v2 格式;schema bump 後值不再被讀
        "index": [
            {"path": p, "symbol": f"sym_{i}", "type": "function", "line": 1}
            for i, p in enumerate(paths)
        ],
    }
    if fingerprint is not None:
        meta["scope_fingerprint"] = fingerprint
    rag.cache_meta_file.write_text(json.dumps(meta), encoding="utf-8")
    rows = np.array([[float(i), 0.0] for i in range(len(paths))], dtype="float32")
    np.savez_compressed(rag.cache_emb_file, embeddings=rows)


def test_legacy_bundle_load_refused_without_fingerprint(tree, clean_scan_cache):
    code_rag = clean_scan_cache
    rag = code_rag.CodeRAG(str(tree))
    _write_legacy_meta(rag, fingerprint=None, paths=["src/core/engine.c"])

    fresh = code_rag.CodeRAG(str(tree))
    assert fresh._load_cache() is False, "沒有 scope_fingerprint 就不准整包 fast load"
    assert fresh.index == []


def test_legacy_bundle_load_refused_on_fingerprint_mismatch(tree, clean_scan_cache):
    code_rag = clean_scan_cache
    rag = code_rag.CodeRAG(str(tree))
    _write_legacy_meta(rag, fingerprint="stale", paths=["src/core/engine.c"])

    fresh = code_rag.CodeRAG(str(tree))
    assert fresh._load_cache() is False


def test_pre_v2_cache_is_rebuilt_with_stderr_reason(tree, clean_scan_cache, capsys):
    """schema v2 起舊快取一律安全重建(§5-2 + §6.2-6 合併 bump,只重建一次)。

    fingerprint 相符也不例外:pre-v2 index entry 缺 qualified_name / backend /
    generation 欄位,留著會變混血索引。stderr 必須講明原因,不得 silent。
    """
    code_rag = clean_scan_cache
    rag = code_rag.CodeRAG(str(tree))
    _write_legacy_meta(
        rag,
        fingerprint=rag.scope.fingerprint,
        paths=["vendor/keep.c", "src/core/engine.c"],
    )

    fresh = code_rag.CodeRAG(str(tree))
    assert fresh._load_cache() is False, "pre-v2 快取必須重建,不得 fast load"
    assert fresh.index == []
    err = capsys.readouterr().err
    assert "schema" in err, "重建原因必須寫到 stderr"


def test_scope_change_recomputes_membership_without_reembedding(tree, tmp_path, monkeypatch,
                                                                clean_scan_cache):
    code_rag = clean_scan_cache
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", False)
    calls: list[str] = []

    def fake_embedding(texts: list[str]) -> list[list[float]]:
        # 新契約(§5-4):build 走 _embed_texts_batched(批次)
        calls.extend(texts)
        return [[1.0, 0.0]] * len(texts)

    _write_scope(tmp_path, monkeypatch, tree, detectors=True)
    first = code_rag.CodeRAG(str(tree))
    monkeypatch.setattr(first, "_embed_texts_batched", fake_embedding)
    first.build_index(verbose=False)
    first_paths = {item["path"] for item in first.index}
    baseline_calls = len(calls)
    assert baseline_calls > 0
    assert "lib/python3.11/stdlib_mod.py" not in first_paths

    # detectors 關掉 → membership 變大,但既有檔案不該重 embed
    _write_scope(tmp_path, monkeypatch, tree, detectors=False)
    second = code_rag.CodeRAG(str(tree))
    monkeypatch.setattr(second, "_embed_texts_batched", fake_embedding)
    calls.clear()
    second.build_index(verbose=False)
    second_paths = {item["path"] for item in second.index}

    assert "lib/python3.11/stdlib_mod.py" in second_paths
    assert first_paths < second_paths
    assert 0 < len(calls) < baseline_calls, "只有新進來的檔案該付 embedding 成本"

    # 再切回來 → 多出來的檔案要退出索引
    _write_scope(tmp_path, monkeypatch, tree, detectors=True)
    third = code_rag.CodeRAG(str(tree))
    monkeypatch.setattr(third, "_embed_texts_batched", fake_embedding)
    third.build_index(verbose=False)
    assert {item["path"] for item in third.index} == first_paths


# ============================================================
# scripts/index_stats.py
# ============================================================


def _run_stats(args, env_extra=None):
    import subprocess

    env = {**os.environ, **(env_extra or {})}
    env.pop("AICODE_ROOT", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "index_stats.py"), *args],
        capture_output=True, text=True, timeout=120, env=env, check=False,
    )


def test_index_stats_requires_explicit_root():
    proc = _run_stats([])
    assert proc.returncode == 2
    assert "AICODE_ROOT" in proc.stderr


@pytest.mark.parametrize("root", ["/", "__nonexistent__"])
def test_index_stats_rejects_unsafe_roots(tmp_path, root):
    target = root if root == "/" else str(tmp_path / "nope")
    proc = _run_stats(["--root", target])
    assert proc.returncode == 2, proc.stdout


def test_index_stats_rejects_non_directory(tmp_path):
    plain = tmp_path / "plain.txt"
    plain.write_text("x\n", encoding="utf-8")
    proc = _run_stats(["--root", str(plain)])
    assert proc.returncode == 2
    assert "不是目錄" in proc.stderr


def test_index_stats_rejects_home(tmp_path):
    home = tmp_path / "fakehome"
    home.mkdir()
    proc = _run_stats(["--root", str(home)], env_extra={"HOME": str(home)})
    assert proc.returncode == 2
    assert "$HOME" in proc.stderr


def test_index_stats_default_output_is_counts_only(tree):
    proc = _run_stats(["--root", str(tree)])
    assert proc.returncode == 0, proc.stderr
    assert "indexed: " in proc.stdout
    for secret in ("site-packages/mypkg", "vendor_env", "engine.c", str(tree)):
        assert secret not in proc.stdout, f"預設輸出洩漏路徑: {secret}"


def test_index_stats_show_paths_is_opt_in(tree):
    proc = _run_stats(["--root", str(tree), "--show-paths"])
    assert proc.returncode == 0, proc.stderr
    assert "src/core/engine.c" in proc.stdout


def test_index_stats_deep_counts_symbols(tree):
    proc = _run_stats(["--root", str(tree), "--deep"])
    assert proc.returncode == 0, proc.stderr
    line = proc.stdout.splitlines()[0]
    assert line.startswith("indexed: ") and "unknown" not in line


def test_index_stats_reports_rule_hits(tree, tmp_path):
    scope_file = tmp_path / "stats-scope.json"
    scope_file.write_text(json.dumps({
        "schema_version": 1,
        "roots": [{"root": str(tree), "exclude": ["vendor_env/**"]}],
    }), encoding="utf-8")
    os.chmod(scope_file, 0o600)
    proc = _run_stats(["--root", str(tree)],
                      env_extra={"AICODE_INDEX_SCOPE_FILE": str(scope_file)})
    assert proc.returncode == 0, proc.stderr
    assert "A' dirs: 1" in proc.stdout
    assert "B1 dirs: 2" in proc.stdout
    assert "B2 dirs: 2" in proc.stdout
    assert "C#1 dirs: 1" in proc.stdout
    assert "vendor_env" not in proc.stdout


# ============================================================
# Review 修復的回歸鎖
# ============================================================


def _seed_cache_with_lazy_holes(rag, rel_paths, *, holes):
    """手動寫一份 per-file 快取,holes 裡的檔案 embedding 全是 []（lazy 模式的產物）。

    直接構造狀態,不依賴「第幾個檔案剛好跨過 lazy 門檻」——那個順序由 os.walk 決定,
    當回歸測試不可靠。
    """
    import code_rag

    file_cache = {}
    for rel in rel_paths:
        path = rag.folder / rel
        symbols = [
            {"path": rel, "symbol": f"sym_{rel}_{i}", "type": "function",
             "line": i + 1, "context": "ctx"}
            for i in range(2)
        ]
        file_cache[rel] = {
            "hash": code_rag.compute_file_hash(path),
            "symbols": symbols,
            "embeddings": [[] for _ in symbols] if rel in holes else [[1.0, 0.0]] * len(symbols),
        }
    # 身分欄位從 production 的單一來源取(code_rag.cache_identity())。
    # 手抄一份的話,新增欄位時這裡會靜默落後 → cache 被拒 → 這條 regression
    # 改走 full rebuild,「還是綠的」卻不再驗 lazy embedding hole 的 backfill。
    rag.cache_meta_file.write_text(json.dumps({
        **code_rag.cache_identity(),
        "scope_fingerprint": rag.scope.fingerprint,
        "row_count": 0,
        "index": [],
        "file_cache": file_cache,
    }), encoding="utf-8")

    # fail-open 防線。少了這條,身分欄位一漂 loader 就拒絕這份 seeded cache →
    # build_index 改走 full rebuild,而 full rebuild 同樣會算出 embeddings、
    # 清掉 holes、restart 也不炸 —— 底下每一條 assertion 都還是綠的,卻完全沒有
    # 驗到 lazy embedding hole 的 backfill。這正是這條 regression 曾經退化的方式。
    loaded = rag._load_file_cache()
    assert set(loaded) == set(rel_paths), (
        "seeded cache 被 loader 拒絕了(身分欄位漂移?);"
        "這條 regression 會靜默退化成 full rebuild"
    )
    return file_cache


@pytest.mark.smoke
def test_seeded_lazy_cache_is_actually_reused_not_rebuilt(tree, monkeypatch,
                                                          clean_scan_cache):
    """焦點版:證明 build_index 真的**復用**了 seeded cache,不是重新 parse。

    seeded symbol 的名字是合成的(`sym_<path>_<i>`),真的去 parse fixture 檔案
    永遠不會產出這種名字 —— 所以它出現在 index 裡,就是「這份 cache 被採用了」
    的直接證據。整條 backfill regression 的前提就是這個,前提沒被驗證的話,
    後面測什麼都不算數。
    """
    code_rag = clean_scan_cache
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", True)
    rag = code_rag.CodeRAG(str(tree))
    kept = sorted(_indexed(rag.scope))
    _seed_cache_with_lazy_holes(rag, kept, holes=set(kept))

    monkeypatch.setattr(rag, "_embed_texts_batched",
                        lambda texts: [[1.0, 0.0]] * len(texts))
    rag.build_index(verbose=False)

    seeded = {item["symbol"] for item in rag.index if item["symbol"].startswith("sym_")}
    assert seeded, (
        "index 裡沒有任何 seeded symbol —— cache 沒被復用,這條測試已經退化成 "
        "full rebuild,不再驗 backfill"
    )


def test_dense_rebuild_backfills_lazy_embedding_holes(tree, monkeypatch, clean_scan_cache):
    """scope 縮小 → dense 模式復用 lazy 快取,空 embedding 必須被補算而不是 fail-loud。

    回歸:原本會拋 "refusing zero padding",而且失敗不寫快取 → 重啟照樣失敗,
    索引永久建不起來。索引縮小正是 index scope 的主要場景。
    """
    code_rag = clean_scan_cache
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", True)
    rag = code_rag.CodeRAG(str(tree))
    kept = sorted(_indexed(rag.scope))
    _seed_cache_with_lazy_holes(rag, kept, holes=set(kept))

    calls: list[str] = []

    def fake_batch(texts: list[str]) -> list[list[float]]:
        calls.extend(texts)
        return [[1.0, 0.0]] * len(texts)

    monkeypatch.setattr(rag, "_embed_texts_batched", fake_batch)
    rag.build_index(verbose=False)

    assert rag._lazy_embed is False
    assert rag.embeddings is not None and rag.embeddings.shape[0] == len(rag.index)
    assert calls, "空洞應該被補算"

    # 快取要被修好,否則重啟又炸一次
    meta = json.loads(rag.cache_meta_file.read_text(encoding="utf-8"))
    holes_left = [
        rel for rel, entry in meta["file_cache"].items()
        for emb in entry["embeddings"] if not emb
    ]
    assert not holes_left, f"快取仍留著空 embedding: {sorted(set(holes_left))}"

    code_rag._INDEX_SCAN_CACHE.clear()
    restart = code_rag.CodeRAG(str(tree))
    monkeypatch.setattr(restart, "_embed_texts_batched",
                        lambda texts: [[1.0, 0.0]] * len(texts))
    restart.build_index(verbose=False)          # 不得再拋


def test_lazy_index_shrunk_by_scope_still_builds(tmp_path, monkeypatch, clean_scan_cache):
    """端到端:大索引跑 lazy → 用 Layer C 縮小 → dense 重建必須成功(含重啟)。"""
    code_rag = clean_scan_cache
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", True)
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED_MAX_SYMBOLS", 20)

    root = tmp_path / "big_tree"
    (root / "keep").mkdir(parents=True)
    (root / "vendor_env").mkdir(parents=True)
    (root / "keep" / "a.py").write_text(
        "".join(f"def keep_{i}(): pass\n" for i in range(5)), encoding="utf-8")
    for i in range(10):
        (root / "vendor_env" / f"v{i}.py").write_text(
            "".join(f"def vend_{i}_{j}(): pass\n" for j in range(10)), encoding="utf-8")

    monkeypatch.setenv("AICODE_INDEX_SCOPE_FILE", str(tmp_path / "absent.json"))
    fake_batch = lambda texts: [[1.0, 0.0]] * len(texts)  # noqa: E731
    first = code_rag.CodeRAG(str(root))
    monkeypatch.setattr(first, "_embed_texts_batched", fake_batch)
    first.build_index(verbose=False)
    assert first._lazy_embed is True, "fixture 沒有真的觸發 lazy 模式,這條就沒在測東西"

    _write_scope(tmp_path, monkeypatch, root, exclude=["vendor_env/**"])
    code_rag._INDEX_SCAN_CACHE.clear()
    second = code_rag.CodeRAG(str(root))
    monkeypatch.setattr(second, "_embed_texts_batched", fake_batch)
    second.build_index(verbose=False)           # 回歸點:這裡原本會拋
    assert second._lazy_embed is False
    assert {item["path"] for item in second.index} == {"keep/a.py"}

    code_rag._INDEX_SCAN_CACHE.clear()
    third = code_rag.CodeRAG(str(root))
    monkeypatch.setattr(third, "_embed_texts_batched", fake_batch)
    third.build_index(verbose=False)            # 重啟也不能炸


def test_scope_file_inside_root_never_enters_index(tree, monkeypatch):
    """index-scope.json 放進 root 也不准進索引 ——「永不進 repo/輸出」是它的契約。"""
    path = tree / "index-scope.json"
    path.write_text(json.dumps(
        {"schema_version": 1, "roots": [{"root": str(tree), "exclude": ["nothing/**"]}]}
    ), encoding="utf-8")
    os.chmod(path, 0o600)
    monkeypatch.setenv("AICODE_INDEX_SCOPE_FILE", str(path))

    scope = load_index_scope(tree)
    assert scope.should_index_file("index-scope.json") is False
    assert "index-scope.json" not in _indexed(scope)

    proc = _run_stats(["--root", str(tree), "--show-paths"],
                      env_extra={"AICODE_INDEX_SCOPE_FILE": str(path)})
    assert proc.returncode == 0, proc.stderr
    assert "index-scope.json" not in proc.stdout


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="需要 POSIX FIFO")
def test_non_regular_files_are_never_read(tmp_path):
    """FIFO / socket 讀下去會永久阻塞 —— 掃描與 --deep 都不准碰。"""
    root = tmp_path / "fifo_tree"
    root.mkdir()
    (root / "real.py").write_text("def real(): pass\n", encoding="utf-8")
    os.mkfifo(root / "blocked.py")

    scope = load_index_scope(root)
    assert scope.should_index_file("blocked.py") is False
    assert _indexed(scope) == {"real.py"}

    import subprocess

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "index_stats.py"),
         "--root", str(root), "--deep"],
        capture_output=True, text=True, timeout=30, check=False,
        env={**os.environ, "AICODE_INDEX_SCOPE_FILE": str(tmp_path / "absent.json")},
    )
    assert proc.returncode == 0, proc.stderr
    assert "indexed: 1 files" in proc.stdout


def test_index_stats_refuses_stale_cached_symbol_count(tmp_path, clean_scan_cache):
    """快取過期時要印 unknown,不能報舊符號數。"""
    import code_rag

    root = tmp_path / "stale_tree"
    root.mkdir()
    target = root / "m.py"
    target.write_text("def one(): pass\ndef two(): pass\n", encoding="utf-8")
    rag = code_rag.CodeRAG(str(root))
    _seed_cache_with_lazy_holes(rag, ["m.py"], holes=set())

    fresh = _run_stats(["--root", str(root)])
    assert "2 (cached) symbols" in fresh.stdout, fresh.stdout

    target.write_text("def one(): pass\ndef two(): pass\ndef three(): pass\n", encoding="utf-8")
    stale = _run_stats(["--root", str(root)])
    assert "unknown symbols" in stale.stdout, stale.stdout


@pytest.mark.parametrize("pattern", ["[z-a]/**", "docs/[9-0]*.md"])
def test_uncompilable_patterns_fail_as_index_scope_error(tree, tmp_path, monkeypatch, pattern):
    _write_scope(tmp_path, monkeypatch, tree, exclude=[pattern])
    with pytest.raises(IndexScopeError):
        load_scope_config(tree)


def test_non_utf8_scope_file_fails_as_index_scope_error(tree, tmp_path, monkeypatch):
    path = tmp_path / "index-scope.json"
    path.write_bytes('{"schema_version":1,"roots":[]}'.encode("utf-16"))
    os.chmod(path, 0o600)
    monkeypatch.setenv("AICODE_INDEX_SCOPE_FILE", str(path))
    with pytest.raises(IndexScopeError):
        load_scope_config(tree)


def test_index_stats_exits_two_on_bad_scope_file(tree, tmp_path):
    bad = tmp_path / "bad-scope.json"
    bad.write_text(json.dumps(
        {"schema_version": 1, "roots": [{"root": str(tree), "exclude": ["[z-a]/**"]}]}
    ), encoding="utf-8")
    os.chmod(bad, 0o600)
    proc = _run_stats(["--root", str(tree)], env_extra={"AICODE_INDEX_SCOPE_FILE": str(bad)})
    assert proc.returncode == 2, proc.stdout
    assert "[FATAL]" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_backfill_failure_leaves_no_partial_index(tree, monkeypatch, clean_scan_cache):
    """backfill 途中 embedding server 掛掉:不能留下半成品索引。

    回歸:原本 backfill 在清理 partial index 的 try/except 之外,失敗後 self.index
    仍非空 → query() 不重建、_refresh_if_stale 也因為 _indexed_file_hashes is None
    直接 return,整個 MCP process 一路用缺 embedding 的索引降級下去。
    """
    code_rag = clean_scan_cache
    rag = code_rag.CodeRAG(str(tree))
    kept = sorted(_indexed(rag.scope))
    _seed_cache_with_lazy_holes(rag, kept, holes=set(kept))

    outage = {"on": True}

    def flaky_batch(texts):
        if outage["on"]:
            raise RuntimeError("embedding server unreachable at test URL")
        return [[1.0, 0.0]] * len(texts)

    monkeypatch.setattr(rag, "_embed_texts_batched", flaky_batch)
    with pytest.raises(RuntimeError, match="embedding server unreachable"):
        rag.build_index(verbose=False)

    assert rag.index == [], "半成品索引沒清掉 → query() 不會重建"
    assert rag.embeddings is None
    assert rag._indexed_file_hashes is None, "_refresh_if_stale 會被舊 hash 卡住"

    # server 恢復:同一個物件必須能重建,而且快取的洞要補好
    outage["on"] = False
    code_rag._INDEX_SCAN_CACHE.clear()
    rag.build_index(verbose=False)
    assert rag.index and rag.embeddings is not None
    meta = json.loads(rag.cache_meta_file.read_text(encoding="utf-8"))
    assert not [
        rel for rel, entry in meta["file_cache"].items()
        for emb in entry["embeddings"] if not emb
    ]


def test_uncompilable_pattern_error_never_leaks_pattern_content(tree, tmp_path, monkeypatch):
    """壞 pattern 的 fatal 訊息不得帶 pattern 內容 —— pattern 就是樹狀結構本身。"""
    secret = "customer_tree_delta"
    _write_scope(tmp_path, monkeypatch, tree, exclude=[f"{secret}/[z-a]/**"])

    with pytest.raises(IndexScopeError) as exc:
        load_scope_config(tree)

    message = str(exc.value)
    assert secret not in message
    assert "bad character range" not in message, "底層 re.error 的訊息也帶片段"
    assert "roots[0]" in message and "exclude[0]" in message, "要能靠位置定位"
    # from None:exception chain 上掛著 re.error 一樣會被 traceback 印出來
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True

    scope_file = tmp_path / "index-scope.json"
    proc = _run_stats(["--root", str(tree)],
                      env_extra={"AICODE_INDEX_SCOPE_FILE": str(scope_file)})
    assert proc.returncode == 2
    assert secret not in proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr
