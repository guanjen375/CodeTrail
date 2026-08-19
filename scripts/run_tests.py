#!/usr/bin/env python3
"""統一測試入口 — 隔離外部 pytest plugin，並加速完整測試。

用途:
    python scripts/run_tests.py            # 依 test file 分片並行跑全部 pytest(最多 8 shard)
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

import fnmatch
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_ROOT = REPO_ROOT / "tests"
MAX_PARALLEL_JOBS = 8
# pytest 預設的 python_files(pyproject 沒有覆寫)。分片必須用同一組樣式並
# 遞迴掃,否則 tests/integration/test_x.py 或 foo_test.py 會被並行模式靜默
# 漏掉——序列 pytest 收得到、並行綠燈卻是假的。
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py")
# pytest 的 norecursedirs 預設值(pyproject 沒有覆寫)+ pytest 自己內建、不在
# norecursedirs 裡的 `__pycache__` 特例。分片必須套用一模一樣的排除規則:
# 多收或少收都是「並行與序列結果漂移」,而漂移的那一邊是靜默的。
# 對照組是真的 `pytest --collect-only`(見 tests/test_test_runner.py)。
NORECURSE_DIR_PATTERNS = (
    "*.egg", ".*", "_darcs", "build", "CVS", "dist", "node_modules", "venv",
    "{arch}", "__pycache__",
)
# 上一輪完整測試量到的每檔耗時(秒)。檔案大小是很差的耗時預測:同樣 30KB,
# 一個是 40 條純函式斷言(0.03s),另一個是 17 條各 fork 一次 aicode(5.6s)。
# 所以跑完一次就把實測寫下來,下一次直接拿來分片;沒有這份檔(第一次跑 / 新增
# 的測試檔)才退回大小啟發式。這份檔在 .pytest_cache 底下,已在 .gitignore。
WEIGHTS_FILE = REPO_ROOT / ".pytest_cache" / "shard_weights.json"
# 大小啟發式 → 秒的換算,由實測校準(拆檔前 test_cli.py 權重 277k ↔ 14.07s)。
HEURISTIC_BYTES_PER_SECOND = 20_000
# junit 只記 setup/call/teardown,不含 module import 與 collection;補一個固定量。
FILE_OVERHEAD_SECONDS = 0.08


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
            raise ValueError(
                f"AICODE_TEST_JOBS 必須是 1..{MAX_PARALLEL_JOBS} 的整數"
            ) from exc
        if not 1 <= jobs <= MAX_PARALLEL_JOBS:
            raise ValueError(f"AICODE_TEST_JOBS 必須是 1..{MAX_PARALLEL_JOBS} 的整數")
        return jobs

    available = os.cpu_count() if cpu_count is None else cpu_count
    return max(1, min(MAX_PARALLEL_JOBS, available or 1))


def _is_norecurse_dir(name: str) -> bool:
    """目錄名是否命中 pytest norecursedirs 預設樣式(fnmatch 語意)。"""
    return any(
        fnmatch.fnmatchcase(name, pattern) for pattern in NORECURSE_DIR_PATTERNS
    )


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
            if any(_is_norecurse_dir(part) for part in directories):
                continue
            found[path] = None
    return sorted(found)


def _weight_key(path: Path) -> str:
    """分片權重的檔案鍵:相對 tests/ 的 posix 路徑;不在 tests/ 底下就用檔名。"""
    try:
        return path.resolve().relative_to(TEST_ROOT).as_posix()
    except ValueError:
        return path.name


def _load_measured_weights(weights_file: Path | None = None) -> dict[str, float]:
    """讀上一輪的實測秒數;檔案不存在 / 壞掉一律當成「沒有資料」。"""
    target = WEIGHTS_FILE if weights_file is None else weights_file
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    measured: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, (int, float)) and value > 0:
            measured[key] = float(value)
    return measured


def _collect_measured_weights(junit_paths: Sequence[Path]) -> dict[str, float]:
    """把各 shard 的 junit XML 併成 {tests/ 相對路徑: 秒}。"""
    totals: dict[str, float] = {}
    for junit_path in junit_paths:
        try:
            root = ElementTree.parse(junit_path).getroot()
        except (OSError, ElementTree.ParseError):
            continue
        for case in root.iter("testcase"):
            classname = case.get("classname") or ""
            if not classname.startswith("tests."):
                continue
            module = classname.split(".")[1] if "." in classname else ""
            if not module:
                continue
            try:
                elapsed = float(case.get("time") or 0.0)
            except ValueError:
                continue
            key = f"{module}.py"
            totals[key] = totals.get(key, 0.0) + elapsed
    return {key: value + FILE_OVERHEAD_SECONDS for key, value in totals.items()}


def _write_measured_weights(weights: Mapping[str, float],
                            weights_file: Path | None = None) -> None:
    if not weights:
        return
    target = WEIGHTS_FILE if weights_file is None else weights_file
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({k: round(v, 3) for k, v in sorted(weights.items())}, indent=1),
            encoding="utf-8",
        )
    except OSError:
        pass  # 權重只是最佳化;寫不進去不該讓測試失敗


def _test_file_weight(path: Path, measured: Mapping[str, float] | None = None) -> float:
    """這個 test module 的預估耗時(秒)。有上一輪實測就用實測,否則用大小啟發式。"""
    if measured:
        hit = measured.get(_weight_key(path))
        if hit is not None:
            return hit
    source = path.read_text(encoding="utf-8")
    raw = len(source.encode("utf-8")) + 8_000 * source.count("subprocess.")
    return raw / HEURISTIC_BYTES_PER_SECOND


def _partition_test_files(paths: Sequence[Path], jobs: int,
                          measured: Mapping[str, float] | None = None) -> list[list[Path]]:
    """Largest-first greedy 分片；同一 test module 不拆，避免重複重型 import。"""
    if jobs < 1:
        raise ValueError("jobs must be positive")
    if not paths:
        return []
    if measured is None:
        measured = _load_measured_weights()
    buckets: list[list[Path]] = [[] for _ in range(min(jobs, len(paths)))]
    loads = [0.0] * len(buckets)
    weighted = sorted(
        ((_test_file_weight(path, measured), path) for path in paths),
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
        junit_paths: list[Path] = []
        log_files = []
        try:
            for index, shard in enumerate(shards, start=1):
                shard_root = temp_root / f"shard-{index}"
                shard_root.mkdir()
                log_path = temp_root / f"shard-{index}.log"
                log_file = open(log_path, "wb")
                junit_path = shard_root / "junit.xml"
                cmd = [
                    sys.executable,
                    "-m",
                    "pytest",
                    *(str(path) for path in shard),
                    "-o",
                    f"cache_dir={shard_root / 'cache'}",
                    f"--basetemp={shard_root / 'tmp'}",
                    f"--junit-xml={junit_path}",
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
                junit_paths.append(junit_path)
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

        # 這一輪的實測耗時 → 下一輪的分片權重。只在全綠時更新:某個 shard 中途
        # 崩掉的話它的 junit 是殘缺的,拿去當權重會讓下一輪分得更差。
        if all(code == 0 for code in return_codes):
            _write_measured_weights(_collect_measured_weights(junit_paths))

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
