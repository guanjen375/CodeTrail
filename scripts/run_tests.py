#!/usr/bin/env python3
"""統一測試入口 — 隔離外部 pytest plugin，並加速完整測試。

用途:
    python scripts/run_tests.py            # 依 test file 分片並行跑全部 pytest
    AICODE_TEST_JOBS=1 python scripts/run_tests.py  # 序列完整測試
    python scripts/run_tests.py -k cli     # 有 args 時等於 pytest -k cli（序列）
    python scripts/run_tests.py -x -v ...  # args 原樣 forward

為什麼存在:
    很多開發機器全域裝了 pytest plugin (ddtrace、xdist、pytest-django 等),
    它們會在 pytest collect 階段自動載入。我們的測試很乾淨,但這些 plugin 不一定。
    一律設 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 並只允許明確列出的 plugin,讓驗收命令
    在所有環境下都 deterministic。

並行只套在「無參數完整測試」：-x / -k / node id 等 pytest 語意因此完全不變。
不依賴 pytest-xdist；每個 shard 都是受控的 ``python -m pytest`` 子行程，且有
獨立 cache / basetemp。Windows 保留既有 ACL shim，固定走序列模式。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_ROOT = REPO_ROOT / "tests"
MAX_PARALLEL_JOBS = 4
# pytest 預設的 python_files(pyproject 沒有覆寫)。分片必須用同一組樣式並
# 遞迴掃,否則 tests/integration/test_x.py 或 foo_test.py 會被並行模式靜默
# 漏掉——序列 pytest 收得到、並行綠燈卻是假的。
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py")
# pytest norecursedirs 的預設集合(加上 __pycache__ / .* 這類永不 collect 的)。
SKIP_DIR_NAMES = frozenset({
    "__pycache__", "node_modules", "build", "dist", "venv", ".venv", "CVS",
})


def _relax_windows_pytest_tmp_acl() -> None:
    """Avoid Python 3.14/Windows tmp dirs that pytest cannot re-open.

    Pytest creates numbered tmp roots with mode 0o700. On some locked-down
    Windows environments this maps to an ACL that immediately denies access
    even to the creating process. The test runner is the only place that needs
    this compatibility shim.
    """
    if os.name != "nt":
        return

    original_mkdir = os.mkdir

    def mkdir(path, mode=0o777, *, dir_fd=None):
        if mode == 0o700:
            mode = 0o777
        if dir_fd is None:
            return original_mkdir(path, mode)
        return original_mkdir(path, mode, dir_fd=dir_fd)

    os.mkdir = mkdir


def _resolve_parallel_jobs(
    environ: Mapping[str, str] | None = None,
    *,
    cpu_count: int | None = None,
) -> int:
    """決定完整測試 shard 數；顯式 env 可重現單執行緒或限縮資源。"""
    env = os.environ if environ is None else environ
    raw = (env.get("AICODE_TEST_JOBS") or "").strip()
    if raw:
        try:
            jobs = int(raw)
        except ValueError as exc:
            raise ValueError("AICODE_TEST_JOBS 必須是 1..4 的整數") from exc
        if not 1 <= jobs <= MAX_PARALLEL_JOBS:
            raise ValueError("AICODE_TEST_JOBS 必須是 1..4 的整數")
        return jobs

    available = os.cpu_count() if cpu_count is None else cpu_count
    return max(1, min(MAX_PARALLEL_JOBS, available or 1))


def _discover_test_files(root: Path | None = None) -> list[Path]:
    """遞迴收集 pytest 會 collect 的測試檔(兩種預設命名),結果去重且排序。"""
    base = TEST_ROOT if root is None else root
    if not base.is_dir():
        return []
    found: dict[Path, None] = {}
    for pattern in TEST_FILE_PATTERNS:
        for path in base.rglob(pattern):
            if not path.is_file():
                continue
            directories = path.relative_to(base).parts[:-1]
            if any(part in SKIP_DIR_NAMES or part.startswith(".")
                   for part in directories):
                continue
            found[path] = None
    return sorted(found)


def _test_file_weight(path: Path) -> int:
    """用檔案大小 + process-bound 測試密度做穩定、免歷史資料的粗估。"""
    source = path.read_text(encoding="utf-8")
    return len(source.encode("utf-8")) + 8_000 * source.count("subprocess.")


def _partition_test_files(paths: Sequence[Path], jobs: int) -> list[list[Path]]:
    """Largest-first greedy 分片；同一 test module 不拆，避免重複重型 import。"""
    if jobs < 1:
        raise ValueError("jobs must be positive")
    if not paths:
        return []
    buckets: list[list[Path]] = [[] for _ in range(min(jobs, len(paths)))]
    loads = [0] * len(buckets)
    weighted = sorted(
        ((_test_file_weight(path), path) for path in paths),
        key=lambda item: (-item[0], item[1].as_posix()),
    )
    for weight, path in weighted:
        target = min(range(len(buckets)), key=lambda index: (loads[index], index))
        buckets[target].append(path)
        loads[target] += weight
    for bucket in buckets:
        bucket.sort()
    return buckets


def _run_parallel(env: Mapping[str, str], jobs: int) -> int:
    test_files = _discover_test_files()
    if not test_files:
        print(
            "[run_tests] 找不到 tests/ 下的測試檔(test_*.py / *_test.py)",
            file=sys.stderr,
        )
        return 2
    shards = _partition_test_files(test_files, jobs)

    print(
        f"[run_tests] PYTEST_DISABLE_PLUGIN_AUTOLOAD=1; "
        f"{len(test_files)} files / {len(shards)} parallel shards",
        flush=True,
    )
    with tempfile.TemporaryDirectory(prefix="codetrail-pytest-") as temp_name:
        temp_root = Path(temp_name)
        processes: list[subprocess.Popen] = []
        log_paths: list[Path] = []
        log_files = []
        try:
            for index, shard in enumerate(shards, start=1):
                shard_root = temp_root / f"shard-{index}"
                shard_root.mkdir()
                log_path = temp_root / f"shard-{index}.log"
                log_file = open(log_path, "wb")
                cmd = [
                    sys.executable,
                    "-m",
                    "pytest",
                    *(str(path) for path in shard),
                    "-o",
                    f"cache_dir={shard_root / 'cache'}",
                    f"--basetemp={shard_root / 'tmp'}",
                ]
                print(
                    f"[run_tests] shard {index}/{len(shards)}: {len(shard)} files",
                    flush=True,
                )
                processes.append(
                    subprocess.Popen(
                        cmd,
                        cwd=str(REPO_ROOT),
                        env=dict(env),
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                    )
                )
                log_paths.append(log_path)
                log_files.append(log_file)

            return_codes = [process.wait() for process in processes]
        except KeyboardInterrupt:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                process.wait()
            return 130
        finally:
            for log_file in log_files:
                log_file.close()

        for index, (log_path, return_code) in enumerate(
            zip(log_paths, return_codes), start=1
        ):
            print(f"\n[run_tests] ===== shard {index} (exit={return_code}) =====")
            print(log_path.read_text(encoding="utf-8", errors="replace"), end="")

    failed = [index for index, code in enumerate(return_codes, start=1) if code != 0]
    if failed:
        print(f"[run_tests] FAILED shards: {failed}", file=sys.stderr)
        return 1
    print(f"[run_tests] PASS: all {len(shards)} shards")
    return 0


def main(argv: list[str]) -> int:
    env = os.environ.copy()
    # 關掉 pytest plugin auto-discovery,避免外部 plugin (ddtrace 之類) 卡 collect 階段
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    # 我們自己不需要任何第三方 plugin。如果未來需要,在這裡明確 enable:
    # env["PYTEST_PLUGINS"] = "pytest_xdist"

    if not argv and os.name != "nt":
        try:
            jobs = _resolve_parallel_jobs(env)
        except ValueError as exc:
            print(f"[run_tests] {exc}", file=sys.stderr)
            return 2
        if jobs > 1:
            return _run_parallel(env, jobs)

    if os.name == "nt":
        tmp_root = REPO_ROOT / ".pytest_cache" / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        env.setdefault("PYTEST_DEBUG_TEMPROOT", str(tmp_root))
        os.environ.update(env)
        _relax_windows_pytest_tmp_acl()
        import pytest

        print(
            f"[run_tests] PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "
            f"{sys.executable} -m pytest {' '.join(argv)}"
        )
        return int(pytest.main(argv))

    cmd = [sys.executable, "-m", "pytest", *argv]
    print(f"[run_tests] PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 {' '.join(cmd)}")
    try:
        return subprocess.call(cmd, cwd=str(REPO_ROOT), env=env)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
