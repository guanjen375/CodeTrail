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


def main(argv: list[str]) -> int:
    env = os.environ.copy()
    # 關掉 pytest plugin auto-discovery,避免外部 plugin (ddtrace 之類) 卡 collect 階段
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    # 我們自己不需要任何第三方 plugin。如果未來需要,在這裡明確 enable:
    # env["PYTEST_PLUGINS"] = "pytest_xdist"

    cmd = [sys.executable, "-m", "pytest", *argv]
    print(f"[run_tests] PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 {' '.join(cmd)}")
    try:
        return subprocess.call(cmd, cwd=str(REPO_ROOT), env=env)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
