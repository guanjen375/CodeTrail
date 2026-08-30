"""scripts/run_tests.py 的純分片邏輯；不從 pytest 內遞迴啟動完整 suite。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import run_tests


def _test_file(path: Path, name: str, body: str) -> Path:
    target = path / name
    target.write_text(body, encoding="utf-8")
    return target


def test_partition_is_deterministic_complete_and_keeps_modules_whole(tmp_path: Path):
    files = [
        _test_file(tmp_path, "test_fast.py", "def test_fast(): pass\n"),
        _test_file(
            tmp_path,
            "test_process.py",
            "import subprocess\ndef test_process(): subprocess.run([])\n",
        ),
        _test_file(tmp_path, "test_large.py", "x = 1\n" * 1_000),
        _test_file(tmp_path, "test_other.py", "def test_other(): pass\n"),
    ]

    first = run_tests._partition_test_files(files, jobs=3)
    second = run_tests._partition_test_files(list(reversed(files)), jobs=3)

    assert first == second
    flattened = [path for shard in first for path in shard]
    assert sorted(flattened) == sorted(files)
    assert len(flattened) == len(set(flattened))


def test_partition_empty_input_is_empty():
    assert run_tests._partition_test_files([], jobs=3) == []


@pytest.mark.parametrize(
    ("env", "cpu_count", "expected"),
    [
        ({}, 1, 1),
        ({}, 64, run_tests.MAX_PARALLEL_JOBS),
        ({"AICODE_TEST_JOBS": "2"}, 64, 2),
        ({"AICODE_TEST_JOBS": " 1 "}, 64, 1),
    ],
)
def test_parallel_job_resolution(env, cpu_count, expected):
    assert run_tests._resolve_parallel_jobs(env, cpu_count=cpu_count) == expected


@pytest.mark.parametrize(
    "value",
    ["0", str(run_tests.MAX_PARALLEL_JOBS + 1), "many", "1.5"],
)
def test_parallel_job_resolution_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="AICODE_TEST_JOBS"):
        run_tests._resolve_parallel_jobs({"AICODE_TEST_JOBS": value}, cpu_count=64)


@pytest.mark.smoke
def test_discovery_covers_nested_dirs_and_both_default_namings(tmp_path: Path):
    """並行分片必須跟序列 pytest 收到同一組檔案。"""
    nested = tmp_path / "integration"
    nested.mkdir()

    expected = {
        _test_file(tmp_path, "test_top.py", "def test_top(): pass\n"),
        _test_file(tmp_path, "legacy_test.py", "def test_legacy(): pass\n"),
        _test_file(nested, "test_nested.py", "def test_nested(): pass\n"),
        _test_file(nested, "nested_test.py", "def test_nested2(): pass\n"),
        # 同時符合兩種樣式 → 只能出現一次
        _test_file(tmp_path, "test_both_test.py", "def test_both(): pass\n"),
    }
    _test_file(tmp_path, "conftest.py", "")
    _test_file(tmp_path, "helper.py", "def helper(): pass\n")

    found = run_tests._discover_test_files(tmp_path)
    assert found == sorted(expected)
    assert len(found) == len(set(found))


@pytest.mark.parametrize(
    "directory",
    [".hidden", "build", "dist", "CVS", "_darcs", "node_modules", "venv",
     "{arch}", "sample.egg"],
)
def test_discovery_applies_pytest_norecursedirs_defaults(tmp_path: Path, directory: str):
    """排除規則必須等同 pytest 預設 norecursedirs,否則並行會多收。"""
    excluded = tmp_path / directory
    excluded.mkdir()
    _test_file(excluded, "test_excluded.py", "def test_excluded(): pass\n")
    kept = _test_file(tmp_path, "test_kept.py", "def test_kept(): pass\n")

    assert run_tests._discover_test_files(tmp_path) == [kept]


@pytest.mark.smoke
def test_discovery_excludes_pycache_like_pytest(tmp_path: Path):
    """`__pycache__` 不在 norecursedirs 裡,但 pytest 內建不收它。

    分片會把檔案路徑「顯式」傳給 pytest,顯式路徑一律會執行,所以這裡多收
    一個 stale 檔就是真的多跑一個測試。
    """
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    _test_file(cache, "test_stale.py", "def test_stale(): pass\n")
    kept = _test_file(tmp_path, "test_kept.py", "def test_kept(): pass\n")

    assert run_tests._discover_test_files(tmp_path) == [kept]


def _pytest_collected_files(root: Path) -> list[Path]:
    """真的跑一次 `pytest --collect-only` 當對照組(只在 tmp fixture 上)。"""
    env = dict(os.environ)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "--rootdir", str(root), str(root)],
        cwd=str(root), env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode in (0, 5), result.stdout + result.stderr
    collected = set()
    for line in result.stdout.splitlines():
        node, sep, _ = line.partition("::")
        if sep and node.endswith(".py"):
            collected.add((root / node.strip()).resolve())
    return sorted(collected)


@pytest.mark.smoke
def test_discovery_matches_real_pytest_collection(tmp_path: Path):
    """分片清單 = 序列 pytest 實際收集到的檔案集合(唯一有意義的對照)。"""
    nested = tmp_path / "integration"
    nested.mkdir()
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "build").mkdir()
    (tmp_path / "sample.egg").mkdir()

    _test_file(tmp_path, "test_top.py", "def test_top(): pass\n")
    _test_file(tmp_path, "legacy_test.py", "def test_legacy(): pass\n")
    _test_file(nested, "test_nested.py", "def test_nested(): pass\n")
    _test_file(tmp_path, "helper.py", "def helper(): pass\n")
    _test_file(tmp_path / "__pycache__", "test_stale.py", "def test_stale(): pass\n")
    _test_file(tmp_path / ".hidden", "test_hidden.py", "def test_hidden(): pass\n")
    _test_file(tmp_path / "build", "test_built.py", "def test_built(): pass\n")
    _test_file(tmp_path / "sample.egg", "test_egg.py", "def test_egg(): pass\n")

    discovered = [path.resolve() for path in run_tests._discover_test_files(tmp_path)]
    assert discovered == _pytest_collected_files(tmp_path)


def test_discovery_of_missing_root_is_empty(tmp_path: Path):
    assert run_tests._discover_test_files(tmp_path / "nope") == []


def test_repo_discovery_is_a_superset_of_top_level_glob():
    top_level = set(run_tests.TEST_ROOT.glob("test_*.py"))
    assert top_level, "repo 的 tests/ 應該有頂層測試檔"
    assert top_level <= set(run_tests._discover_test_files())


# ---------------------------------------------------------------------------
# marker 選取的分片契約(AGENTS.md §1.1 的 smoke 走這條路)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
@pytest.mark.parametrize("argv,expected", [
    (["-m", "smoke"], "smoke"),
    (["-msmoke"], "smoke"),
    (["-m", "smoke and not slow"], "smoke and not slow"),
])
def test_marker_selection_recognises_pure_marker_runs(argv, expected):
    assert run_tests.marker_selection(argv) == expected


@pytest.mark.smoke
@pytest.mark.parametrize("argv", [
    [],                          # 完整測試走自己的分支
    ["-m", "smoke", "-x"],       # -x 的 exitfirst 在分片下不等價
    ["-x", "-m", "smoke"],
    ["-k", "cli"],
    ["-m"],                      # 缺運算式:交給 pytest 自己報 usage error
    ["-m", "smoke", "tests/test_cli.py"],
    ["--lf"],
    ["-msmoke", "-v"],
])
def test_marker_selection_refuses_anything_else(argv):
    """只認純 marker 選取。多認一個旗標就多一次「分片下還等價嗎」的判斷,
    而判斷錯的後果是綠燈假象,不是報錯。"""
    assert run_tests.marker_selection(argv) is None


@pytest.mark.smoke
def test_marker_and_full_runs_keep_separate_weight_files():
    """`-m smoke` 的秒數不能寫進完整測試的權重檔——smoke 子集的耗時拿去分片
    完整測試,會讓同一個檔被嚴重低估。"""
    full = run_tests.weights_file_for(None)
    smoke = run_tests.weights_file_for("smoke")
    assert full == run_tests.WEIGHTS_FILE
    assert smoke != full
    assert smoke.parent == full.parent
    assert "smoke" in smoke.name


@pytest.mark.smoke
def test_weights_file_never_escapes_the_cache_dir():
    """marker 運算式是使用者輸入;不能讓它變成路徑。"""
    hostile = run_tests.weights_file_for("../../etc/passwd")
    assert hostile.parent == run_tests.WEIGHTS_FILE.parent
    assert "/" not in hostile.name and ".." not in hostile.name


@pytest.mark.smoke
@pytest.mark.parametrize("codes,selection,expected_exit,expected_failed", [
    ([0, 0, 0], None, 0, []),
    ([0, 1, 0], None, 1, [2]),
    ([0, 5, 0], None, 1, [2]),        # 完整測試的 shard 收不到測試 = collection 壞了
    ([0, 5, 0], "smoke", 0, []),      # marker 模式下那是正常的
    ([5, 5, 5], "smoke", 5, []),      # 但全空 = 0 collected,依 §2.2 不是通過
    ([5, 1, 5], "smoke", 1, [2]),
    ([0, 2, 0], "smoke", 1, [2]),     # 被中斷
    ([0, 3, 0], "smoke", 1, [2]),     # internal error
])
def test_shard_outcomes_never_turn_zero_collected_into_a_pass(
    codes, selection, expected_exit, expected_failed
):
    exit_code, failed = run_tests.summarize_shard_outcomes(codes, selection)
    assert (exit_code, failed) == (expected_exit, expected_failed)


@pytest.mark.smoke
def test_weights_survive_a_shard_that_only_had_test_failures(tmp_path: Path):
    """紅燈期正是最常重跑的時候。exit 1 的 junit 是完整的,那一輪的實測要留下來,
    否則整個開發期都在用檔案大小猜配重。"""
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite name="pytest" tests="2" failures="1" errors="0" '
        'skipped="0" time="3.0">'
        '<testcase classname="tests.test_alpha" name="ok" time="1.0"/>'
        '<testcase classname="tests.test_alpha" name="bad" time="2.0">'
        '<failure message="boom">boom</failure></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    assert 1 in run_tests.COMPLETED_SESSION_CODES, "exit 1 的 junit 是完整的"
    weights = run_tests._collect_measured_weights([junit])
    assert weights["test_alpha.py"] == pytest.approx(
        3.0 + run_tests.FILE_OVERHEAD_SECONDS
    )


@pytest.mark.smoke
@pytest.mark.parametrize("code", [2, 3, 4])
def test_incomplete_sessions_are_not_trusted_for_weights(code):
    """被中斷 / internal error / usage error 的 shard,junit 是半截的。"""
    assert code not in run_tests.COMPLETED_SESSION_CODES


@pytest.mark.smoke
def test_files_that_ran_but_selected_nothing_still_get_a_weight(tmp_path: Path):
    """marker 模式下大量檔案一條都沒被選中,但它們仍然被 import 過。
    沒有紀錄的話下一輪會退回大小啟發式,把 smoke 掛零的大檔估成好幾秒。"""
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite name="pytest" tests="1" failures="0" errors="0" '
        'skipped="0" time="1.0">'
        '<testcase classname="tests.test_alpha" name="ok" time="1.0"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    ran = [run_tests.TEST_ROOT / "test_alpha.py", run_tests.TEST_ROOT / "test_quiet.py"]
    weights = run_tests._collect_measured_weights([junit], ran)
    assert weights["test_alpha.py"] == pytest.approx(
        1.0 + run_tests.FILE_OVERHEAD_SECONDS
    )
    assert weights["test_quiet.py"] == pytest.approx(run_tests.FILE_OVERHEAD_SECONDS)
