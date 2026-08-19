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
