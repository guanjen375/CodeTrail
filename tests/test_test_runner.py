"""scripts/run_tests.py 的純分片邏輯；不從 pytest 內遞迴啟動完整 suite。"""
from __future__ import annotations

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


@pytest.mark.parametrize("value", ["0", "5", "many", "1.5"])
def test_parallel_job_resolution_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="AICODE_TEST_JOBS"):
        run_tests._resolve_parallel_jobs({"AICODE_TEST_JOBS": value}, cpu_count=64)


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


def test_discovery_does_not_invent_extra_exclusions(tmp_path: Path):
    """pytest 不排除的目錄(例如 __pycache__)也不能被我們自己排掉。"""
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    stale = _test_file(cache, "test_stale.py", "def test_stale(): pass\n")
    kept = _test_file(tmp_path, "test_kept.py", "def test_kept(): pass\n")

    assert run_tests._discover_test_files(tmp_path) == sorted([stale, kept])


def test_discovery_of_missing_root_is_empty(tmp_path: Path):
    assert run_tests._discover_test_files(tmp_path / "nope") == []


def test_repo_discovery_is_a_superset_of_top_level_glob():
    top_level = set(run_tests.TEST_ROOT.glob("test_*.py"))
    assert top_level, "repo 的 tests/ 應該有頂層測試檔"
    assert top_level <= set(run_tests._discover_test_files())
