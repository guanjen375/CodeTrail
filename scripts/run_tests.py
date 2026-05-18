#!/usr/bin/env python3
"""統一測試入口 — 不會被使用者環境裡自動載入的 pytest plugin 汙染。

用途:
    python scripts/run_tests.py            # 跑全部 pytest
    python scripts/run_tests.py -k cli     # 等於 pytest -k cli
    python scripts/run_tests.py -x -v ...  # 任何 args 都直接 forward

為什麼存在:
    很多開發機器全域裝了 pytest plugin (ddtrace、xdist、pytest-django 等),
    它們會在 pytest collect 階段自動載入。我們的測試很乾淨,但這些 plugin 不一定。
    一律設 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 並只允許明確列出的 plugin,讓驗收命令
    在所有環境下都 deterministic。

注意:
    這個 script 自己不能用 pytest 跑(會無限遞迴的觀感)。
    跑它就是直接呼叫 python -m pytest。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


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


def main(argv: list[str]) -> int:
    env = os.environ.copy()
    # 關掉 pytest plugin auto-discovery,避免外部 plugin (ddtrace 之類) 卡 collect 階段
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    # 我們自己不需要任何第三方 plugin。如果未來需要,在這裡明確 enable:
    # env["PYTEST_PLUGINS"] = "pytest_xdist"

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
